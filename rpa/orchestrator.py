"""Orquestación: preparar -> descargar -> enviar -> limpiar. El driver de Hosvital se inyecta."""
from __future__ import annotations

import shutil
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

from .config import Config
from .db import AuditDB
from .excel_loader import JobSpec, load_jobs
from .mailer import GraphMailer, render_message
from .pdf_tools import encrypt_pdf, sha256_file, verify_pdf
from .types import DocResult, PatientResult, RunAborted, SessionExpired


class Driver(Protocol):
    def ensure_ready(self, fecha: date) -> None: ...
    def download_patient(self, job: JobSpec, outdir: Path) -> PatientResult: ...
    def recover(self) -> None: ...


class Orchestrator:
    def __init__(self, cfg: Config, db: AuditDB, driver: Driver | None = None, mailer: GraphMailer | None = None,
                 sleep: Callable[[float], None] = time.sleep, screenshot: Callable[[Path], None] | None = None,
                 log: Callable[[str], None] = print):
        self.cfg, self.db, self.driver, self.mailer = cfg, db, driver, mailer
        self._sleep, self._screenshot, self.log = sleep, screenshot, log

    # ---------- rutas ----------
    def work(self) -> Path:
        return self.cfg.path("paths.work_dir")

    def staging(self, run_id: int) -> Path:
        return self.work() / "staging" / self.db.run_fecha(run_id)

    def job_dir(self, run_id: int, row) -> Path:
        return self.staging(run_id) / f"{row['tdoc']}_{row['documento']}_ing{row['ingreso']}"

    # ---------- 1) preparar ----------
    def prepare(self, excel_path: Path) -> int:
        res = load_jobs(excel_path, self.cfg.get("filters.estados"), self.cfg.get("filters.especialidades"),
                        bool(self.cfg.get("filters.skip_if_bad_email", True)))
        fechas = Counter(j.fecha for j in res.jobs)
        fecha = fechas.most_common(1)[0][0] if fechas else date.today()
        run_id, created = self.db.open_run(fecha.isoformat(), res.sha256, Path(excel_path).name)
        if created:
            for o in res.omitidas:
                self.db.add_skipped(run_id, o)
        repetidos = 0
        for spec in res.jobs:
            job_id = self.db.add_job(run_id, spec)
            if self.db.get_job(job_id)["status"] == "PENDIENTE" and self.db.already_sent(spec, run_id):
                self.db.set_job(job_id, status="YA_ENVIADO", detalle="Ya se envió en una corrida anterior del mismo día")
                repetidos += 1
        if repetidos:
            self.db.event(run_id, "WARN", "YA_ENVIADOS", f"{repetidos} pacientes ya enviados en otra corrida; se omiten")
        for aviso in res.advertencias:
            self.db.event(run_id, "WARN", "EXCEL", aviso)
        if len(fechas) > 1:
            self.db.event(run_id, "WARN", "EXCEL_FECHAS",
                          f"El Excel trae varias fechas de cita: {sorted(str(f) for f in fechas)}")
        self.log(f"Corrida {run_id} ({'nueva' if created else 'reanudada'}): {len(res.jobs)} pacientes, "
                 f"{len(res.omitidas)} filas omitidas")
        return run_id

    # ---------- 2) descargar ----------
    def download(self, run_id: int) -> None:
        assert self.driver is not None, "download requiere un driver"
        max_consec = int(self.cfg.get("run.max_consecutive_errors", 5))
        consec = 0
        for row in self.db.jobs_by_status(run_id, ["PENDIENTE", "DESCARGANDO"]):
            healthy = self._download_one(run_id, row)
            consec = 0 if healthy else consec + 1
            if consec >= max_consec:
                self.db.event(run_id, "ERROR", "ABORTADA", f"{consec} pacientes seguidos con fallo del driver")
                raise RunAborted(f"{consec} pacientes seguidos con fallo del driver; se detiene la corrida")

    def _spec(self, row) -> JobSpec:
        return JobSpec(tdoc=row["tdoc"], documento=row["documento"], ingreso=row["ingreso"],
                       fecha=date.fromisoformat(row["fecha_cita"]), email=row["email"],
                       nombre=row["nombre"] or "", especialidad=row["especialidad"] or "", estado="")

    def _download_one(self, run_id: int, row) -> bool:
        """Devuelve False si el DRIVER falló (cuenta para el corte por errores seguidos)."""
        job_id, spec = row["id"], self._spec(row)
        outdir = self.job_dir(run_id, row)
        max_attempts = int(self.cfg.get("run.max_attempts_per_patient", 2))
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            self.db.bump_attempts(job_id)
            self.db.set_job(job_id, status="DESCARGANDO")
            shutil.rmtree(outdir, ignore_errors=True)
            outdir.mkdir(parents=True, exist_ok=True)
            self.db.clear_documents(job_id)
            try:
                self.driver.ensure_ready(spec.fecha)
                res = self.driver.download_patient(spec, outdir)
            except SessionExpired as e:
                last_err = "sesion_expirada"
                self.db.event(run_id, "WARN", "SESION_EXPIRADA", f"Intento {attempt}: se reintenta ({e})", job_id)
                continue
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                self.db.event(run_id, "ERROR", "DRIVER", f"Intento {attempt}: {last_err}", job_id)
                self._take_screenshot(run_id, job_id)
                try:
                    self.driver.recover()
                except Exception as e2:  # noqa: BLE001
                    self.db.event(run_id, "ERROR", "RECOVER", repr(e2), job_id)
                continue

            status, detalle, retry = self._evaluate(run_id, job_id, spec, res)
            if retry and attempt < max_attempts:
                self.db.event(run_id, "WARN", "REINTENTO", f"{detalle}", job_id)
                continue
            self.db.set_job(job_id, status=status, detalle=detalle)
            self.log(f"  {spec.tdoc} {spec.documento} ing {spec.ingreso}: {status}")
            return True
        self.db.set_job(job_id, status="ERROR_DESCARGA", detalle=last_err)
        self.log(f"  {spec.tdoc} {spec.documento} ing {spec.ingreso}: ERROR_DESCARGA ({last_err})")
        return False

    def _evaluate(self, run_id: int, job_id: int, spec: JobSpec, res: PatientResult) -> tuple[str, str, bool]:
        if res.estado != "OK":
            return res.estado, res.detalle, False
        require_doc = bool(self.cfg.get("verificacion.requerir_documento_en_pdf", True))
        hc_ok = False
        problemas: list[str] = []
        retry = False
        verif_fallo = False
        for dr in res.docs:
            dt = self.cfg.doc_by_key(dr.tipo)
            if dr.estado == "NO_EXISTE":
                self.db.add_document(job_id, dr.tipo, "NO_EXISTE", detalle=dr.detalle)
                continue
            if dr.estado == "ERROR":
                self.db.add_document(job_id, dr.tipo, "ERROR", detalle=dr.detalle)
                problemas.append(f"{dr.tipo}:{dr.detalle}")
                retry = True
                continue
            for path, folio in dr.archivos:
                vr = verify_pdf(path, spec.documento, spec.nombre, require_doc)
                self.db.add_document(
                    job_id, dr.tipo, "DESCARGADO" if vr.ok else "VERIFICACION_FALLIDA", archivo=str(path),
                    folio=folio, sha256=sha256_file(path), paginas=vr.paginas, verificado=vr.ok,
                    detalle=vr.motivo + ("" if vr.nombre_ok in (None, True) else ";nombre_no_coincide"))
                if vr.ok:
                    hc_ok = hc_ok or dt.obligatorio
                else:
                    verif_fallo = True
                    problemas.append(f"{dr.tipo}:{vr.motivo}")
        obligatorios = [d.key for d in self.cfg.docs if d.obligatorio]
        if verif_fallo:
            return "ERROR_VERIFICACION", "; ".join(problemas), False
        if problemas:
            return "ERROR_DESCARGA", "; ".join(problemas), retry
        if obligatorios and not hc_ok:
            return "SIN_HC", "No se obtuvo el documento obligatorio (" + ",".join(obligatorios) + ")", False
        return "DESCARGADO", "", False

    def _take_screenshot(self, run_id: int, job_id: int) -> None:
        if not (self._screenshot and self.cfg.get("run.screenshot_on_error", True)):
            return
        try:
            folder = self.work() / "screens"
            folder.mkdir(parents=True, exist_ok=True)
            self._screenshot(folder / f"run{run_id}_job{job_id}_{int(time.time())}.png")
        except Exception as e:  # noqa: BLE001
            self.db.event(run_id, "WARN", "SCREENSHOT", repr(e), job_id)

    # ---------- 3) enviar ----------
    def send(self, run_id: int) -> None:
        assert self.mailer is not None, "send requiere un mailer"
        for row in self.db.jobs_by_status(run_id, ["ENVIANDO"]):
            # una corrida anterior murió a mitad del envío: no se sabe si salió el correo
            self.db.set_job(row["id"], status="ENVIO_INCIERTO", detalle="Interrumpido durante el envío; revisar Enviados")
            self.db.event(run_id, "ERROR", "ENVIO_INCIERTO", "Envío interrumpido", row["id"])
        interval = float(self.cfg.get("mail.send_interval_seconds", 3))
        for row in self.db.jobs_by_status(run_id, ["DESCARGADO"]):
            if self._send_one(run_id, row) and not self.mailer.dry_run:
                self._sleep(interval)

    def _send_one(self, run_id: int, row) -> bool:
        job_id = row["id"]
        docs = [d for d in self.db.documents(job_id) if d["verificado"] and d["estado"] == "DESCARGADO"]
        email = row["email"]
        if not docs or not email:
            self.db.set_job(job_id, status="ERROR_ENVIO", detalle="sin documentos verificados o sin correo")
            return False
        enc_dir = self.job_dir(run_id, row) / "enc"
        por_tipo = Counter(d["tipo"] for d in docs)
        nombres_usados: dict[str, int] = {}
        adjuntos: list[tuple[str, bytes]] = []
        try:
            for d in docs:
                base = self.cfg.doc_by_key(d["tipo"]).archivo
                if por_tipo[d["tipo"]] > 1 and d["folio"]:
                    base += f"_folio{d['folio']}"
                # Varios PDFs del MISMO check (p. ej. ÓRDENES con dos reportes para el mismo folio):
                # el staging los distingue por sufijo (_2, _3); aquí se replica para no sobreescribirlos.
                nombres_usados[base] = nombres_usados.get(base, 0) + 1
                if nombres_usados[base] > 1:
                    base = f"{base}_{nombres_usados[base]}"
                dst = enc_dir / f"{base}.pdf"
                encrypt_pdf(Path(d["archivo"]), dst, row["documento"])
                adjuntos.append((dst.name, dst.read_bytes()))
        except Exception as e:  # noqa: BLE001
            self.db.set_job(job_id, status="ERROR_ENVIO", detalle=f"cifrado: {type(e).__name__}: {e}")
            self.db.event(run_id, "ERROR", "CIFRADO", repr(e), job_id)
            return False

        subject, body = render_message(self.cfg, row["nombre"] or "", date.fromisoformat(row["fecha_cita"]))
        names = [n for n, _ in adjuntos]
        dry = self.mailer.dry_run
        if not dry:
            self.db.set_job(job_id, status="ENVIANDO")
        email_id = self.db.add_email(job_id, email, "SIMULANDO" if dry else "ENVIANDO", names)
        res = self.mailer.send(email, subject, body, adjuntos)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        if res.ok and res.dry_run:
            self.db.set_email(email_id, estado="SIMULADO", enviado_at=now)
            self.log(f"  [simulado] {row['documento']} -> {email} ({len(adjuntos)} adjuntos)")
            return False  # el estado del paciente no cambia: sigue pendiente de envío real
        if res.ok:
            self.db.set_email(email_id, estado="ENVIADO", enviado_at=now, request_id=res.request_id)
            self.db.set_job(job_id, status="ENVIADO", detalle="")
            self.log(f"  enviado {row['documento']} -> {email}")
            return True
        estado_email, estado_job = ("INCIERTO", "ENVIO_INCIERTO") if res.uncertain else ("ERROR", "ERROR_ENVIO")
        self.db.set_email(email_id, estado=estado_email, request_id=res.request_id, detalle=res.error)
        self.db.set_job(job_id, status=estado_job, detalle=res.error)
        self.db.event(run_id, "ERROR", estado_job, res.error, job_id)
        return False

    # ---------- 4) limpiar ----------
    def reset_test_env(self) -> None:
        """Entorno de prueba: vacía la BD y las carpetas temporales para repetir con los mismos pacientes.

        Solo con `entorno: prueba`; así ni una orden manual puede borrar la auditoría de producción, que es
        la que impide reenviar un ingreso ya enviado. La auditoría en Excel no se toca.
        """
        if self.cfg.get("entorno", "produccion") != "prueba":
            raise RuntimeError("reset-db solo se permite con 'entorno: prueba' en la configuración")
        self.db.reset()
        for sub in ("staging", "quarantine", "screens"):
            shutil.rmtree(self.work() / sub, ignore_errors=True)

    def cleanup(self, run_id: int) -> dict:
        borrados, conservados = 0, 0
        if self.cfg.get("cleanup.delete_files_after_send", True):
            for row in self.db.all_jobs(run_id):
                d = self.job_dir(run_id, row)
                if row["status"] == "ENVIADO":
                    if d.exists():
                        shutil.rmtree(d, ignore_errors=True)
                        borrados += 1
                elif d.exists():
                    conservados += 1
            try:
                self.staging(run_id).rmdir()  # solo si quedó vacía
            except OSError:
                pass
        if self.cfg.get("cleanup.purge_quarantine", True):
            shutil.rmtree(self.work() / "quarantine", ignore_errors=True)
        shutil.rmtree(self.work() / "screens", ignore_errors=True)  # las capturas pueden mostrar datos de pacientes
        self.db.event(run_id, "INFO", "LIMPIEZA", f"carpetas borradas={borrados}, conservadas={conservados}")
        return {"borrados": borrados, "conservados": conservados}
