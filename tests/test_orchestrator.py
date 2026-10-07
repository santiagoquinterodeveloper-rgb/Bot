import pytest
from pypdf import PdfReader

from rpa.db import AuditDB
from rpa.mailer import SendResult
from rpa.orchestrator import Orchestrator
from rpa.types import DocResult, DriverError, PatientResult, RunAborted, SessionExpired
from tests.conftest import make_pdf


class FakeMailer:
    def __init__(self, results=None, dry_run=False):
        self.dry_run, self.sent, self.results = dry_run, [], list(results or [])

    def send(self, to, subject, html, attachments):
        self.sent.append((to, subject, [n for n, _ in attachments], dict(attachments)))
        if self.dry_run:
            return SendResult(True, dry_run=True)
        return self.results.pop(0) if self.results else SendResult(True, status_code=202, request_id="r1")


class FakeDriver:
    """Genera PDFs como lo haría Hosvital. `script[doc]` permite forzar escenarios."""

    def __init__(self, pdf_factory, script=None):
        self.pdf, self.script, self.calls, self.recovered = pdf_factory, script or {}, [], 0

    def ensure_ready(self, fecha):
        pass

    def recover(self):
        self.recovered += 1

    def download_patient(self, job, outdir):
        self.calls.append(job.documento)
        beh = self.script.get(job.documento)
        if callable(beh):
            beh = beh(len(self.calls))
        if isinstance(beh, PatientResult):
            return beh
        texto = f"Paciente {job.documento}"
        if beh == "ajeno":
            texto = "Paciente 000"
        hc = self.pdf(outdir / "HC.pdf", texto)
        docs = [DocResult("HC", "DESCARGADO", [(hc, None)]),
                DocResult("INC", "NO_EXISTE", detalle="sin incapacidad"),
                DocResult("FOR", "DESCARGADO", [(self.pdf(outdir / "FOR_71.pdf", texto), "71")]),
                DocResult("ORD", "DESCARGADO", [(self.pdf(outdir / "ORD_1.pdf", texto), "1"),
                                                (self.pdf(outdir / "ORD_2.pdf", texto), "2")])]
        return PatientResult("OK", docs=docs)


def make_orch(cfg, driver, mailer):
    db = AuditDB(cfg.path("paths.db_path"))
    return db, Orchestrator(cfg, db, driver, mailer, sleep=lambda _: None, log=lambda m: None)


def status_of(db, run_id, documento, ingreso=None):
    rows = [r for r in db.all_jobs(run_id) if r["documento"] == documento and (ingreso is None or r["ingreso"] == ingreso)]
    return rows[0]["status"]


def test_flujo_completo_envio_real_y_limpieza(cfg, excel_file, pdf_factory):
    mailer = FakeMailer()
    db, orch = make_orch(cfg, FakeDriver(pdf_factory), mailer)
    run = orch.prepare(excel_file)
    orch.download(run)
    assert status_of(db, run, "123456789", 1) == "DESCARGADO"
    orch.send(run)
    assert status_of(db, run, "123456789", 1) == "ENVIADO"
    assert len(mailer.sent) == 3                                    # tres ingresos habilitados = tres correos
    to, subject, nombres, data = next(m for m in mailer.sent if m[0] == "p1@gmail.com")
    assert sorted(nombres) == ["Formulacion.pdf", "Historia_clinica.pdf", "Ordenes_medicas_folio1.pdf",
                               "Ordenes_medicas_folio2.pdf"]
    reader = PdfReader(__import__("io").BytesIO(data["Historia_clinica.pdf"]))
    assert reader.is_encrypted and int(reader.decrypt("123456789")) != 0   # contraseña = documento
    assert "ENVIADO" in {e["estado"] for e in db.table("emails", run)}
    res = orch.cleanup(run)
    assert res["borrados"] == 3 and not orch.staging(run).exists()


def test_dry_run_no_cambia_estado_ni_borra(cfg, excel_file, pdf_factory):
    mailer = FakeMailer(dry_run=True)
    db, orch = make_orch(cfg, FakeDriver(pdf_factory), mailer)
    run = orch.prepare(excel_file)
    orch.download(run)
    orch.send(run)
    assert status_of(db, run, "123456789", 1) == "DESCARGADO"
    assert {e["estado"] for e in db.table("emails", run)} == {"SIMULADO"}
    assert orch.cleanup(run)["borrados"] == 0


def test_paciente_no_encontrado_y_documento_ajeno(cfg, excel_file, pdf_factory):
    script = {"123456789": PatientResult("PACIENTE_NO_ENCONTRADO", "Mensaje: no existe"), "987654321": "ajeno"}
    mailer = FakeMailer()
    db, orch = make_orch(cfg, FakeDriver(pdf_factory, script), mailer)
    run = orch.prepare(excel_file)
    orch.download(run)
    orch.send(run)
    assert status_of(db, run, "123456789", 1) == "PACIENTE_NO_ENCONTRADO"
    assert status_of(db, run, "987654321") == "ERROR_VERIFICACION"      # PDF de otra persona: jamás se envía
    assert all(m[0] != "mp@outlook.com" for m in mailer.sent)


def test_sesion_expirada_reintenta_el_mismo_paciente(cfg, excel_file, pdf_factory):
    def beh(n):
        if n == 1:
            raise SessionExpired()
        return None

    drv = FakeDriver(pdf_factory, {"123456789": beh})
    db, orch = make_orch(cfg, drv, FakeMailer())
    run = orch.prepare(excel_file)
    orch.download(run)
    assert status_of(db, run, "123456789", 1) == "DESCARGADO"
    assert drv.calls.count("123456789") >= 2


def test_sin_hc_no_se_envia(cfg, excel_file, pdf_factory):
    res = PatientResult("OK", docs=[DocResult("HC", "NO_EXISTE", detalle="folio abierto")])
    db, orch = make_orch(cfg, FakeDriver(pdf_factory, {"987654321": res}), FakeMailer())
    run = orch.prepare(excel_file)
    orch.download(run)
    assert status_of(db, run, "987654321") == "SIN_HC"


def test_errores_seguidos_detienen_la_corrida(cfg, excel_file, pdf_factory):
    def boom(n):
        raise DriverError("ventana inesperada")

    script = {d: boom for d in ("123456789", "987654321")}
    cfg.raw["run"]["max_consecutive_errors"] = 2
    cfg.raw["run"]["max_attempts_per_patient"] = 1
    drv = FakeDriver(pdf_factory, script)
    db, orch = make_orch(cfg, drv, FakeMailer())
    run = orch.prepare(excel_file)
    with pytest.raises(RunAborted):
        orch.download(run)
    assert drv.recovered >= 2
    assert status_of(db, run, "123456789", 1) == "ERROR_DESCARGA"


def test_envio_incierto_no_se_reintenta(cfg, excel_file, pdf_factory):
    mailer = FakeMailer([SendResult(False, uncertain=True, status_code=500, error="servidor_incierto:500")])
    db, orch = make_orch(cfg, FakeDriver(pdf_factory), mailer)
    run = orch.prepare(excel_file)
    orch.download(run)
    orch.send(run)
    assert status_of(db, run, "123456789", 1) == "ENVIO_INCIERTO"
    n = len(mailer.sent)
    orch.send(run)                                    # segunda pasada: no vuelve a enviar ese ingreso
    assert len(mailer.sent) == n
    job = next(j for j in db.all_jobs(run) if j["documento"] == "123456789" and j["ingreso"] == 1)
    assert [e["estado"] for e in db.table("emails", run) if e["job_id"] == job["id"]] == ["INCIERTO"]


def test_reanudar_no_duplica_y_auditoria(cfg, excel_file, pdf_factory):
    from rpa.audit_export import export_audit

    db, orch = make_orch(cfg, FakeDriver(pdf_factory), FakeMailer())
    r1 = orch.prepare(excel_file)
    r2 = orch.prepare(excel_file)
    assert r1 == r2 and len(db.all_jobs(r1)) == 3 and len(db.table("skipped", r1)) == 4
    path = export_audit(db, r1, cfg.path("paths.audit_dir"))
    assert path.exists()


def test_excel_guardado_de_nuevo_no_duplica_envios(cfg, excel_file, pdf_factory):
    import pandas as pd

    mailer = FakeMailer()
    db, orch = make_orch(cfg, FakeDriver(pdf_factory), mailer)
    r1 = orch.prepare(excel_file)
    orch.download(r1)
    orch.send(r1)
    enviados = len(mailer.sent)
    assert enviados == 3

    df = pd.read_excel(excel_file, dtype=str)           # el Excel cambia (otra huella) pero trae los mismos pacientes
    df.loc[len(df)] = df.iloc[3]
    df.to_excel(excel_file, index=False)
    r2 = orch.prepare(excel_file)
    assert r2 != r1
    assert {j["status"] for j in db.all_jobs(r2)} == {"YA_ENVIADO"}
    orch.download(r2)
    orch.send(r2)
    assert len(mailer.sent) == enviados                 # nadie recibe dos veces
