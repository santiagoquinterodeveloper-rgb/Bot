"""Auditoría y estado persistente (SQLite). Permite reanudar una corrida interrumpida."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, fecha TEXT, excel_sha TEXT, excel_name TEXT,
  started_at TEXT, finished_at TEXT, status TEXT, UNIQUE(fecha, excel_sha));
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, run_id INTEGER, tdoc TEXT, documento TEXT, ingreso INTEGER,
  fecha_cita TEXT, email TEXT, nombre TEXT, especialidad TEXT, filas TEXT,
  status TEXT, detalle TEXT DEFAULT '', intentos INTEGER DEFAULT 0, updated_at TEXT,
  UNIQUE(run_id, tdoc, documento, ingreso));
CREATE TABLE IF NOT EXISTS documents(
  id INTEGER PRIMARY KEY, job_id INTEGER, tipo TEXT, estado TEXT, archivo TEXT, folio TEXT,
  sha256 TEXT, paginas INTEGER, verificado INTEGER DEFAULT 0, detalle TEXT DEFAULT '', created_at TEXT);
CREATE TABLE IF NOT EXISTS emails(
  id INTEGER PRIMARY KEY, job_id INTEGER, destinatario TEXT, estado TEXT, enviado_at TEXT,
  request_id TEXT, adjuntos TEXT, detalle TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS skipped(
  id INTEGER PRIMARY KEY, run_id INTEGER, fila INTEGER, motivo TEXT, tdoc TEXT,
  documento TEXT, ingreso TEXT, email TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT, run_id INTEGER, job_id INTEGER, nivel TEXT, codigo TEXT, mensaje TEXT);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AuditDB:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- corridas ----
    def open_run(self, fecha: str, excel_sha: str, excel_name: str) -> tuple[int, bool]:
        row = self.conn.execute(
            "SELECT id FROM runs WHERE fecha=? AND excel_sha=?", (fecha, excel_sha)).fetchone()
        if row:
            return int(row["id"]), False
        cur = self.conn.execute(
            "INSERT INTO runs(fecha, excel_sha, excel_name, started_at, status) VALUES(?,?,?,?,?)",
            (fecha, excel_sha, excel_name, _now(), "ABIERTA"))
        self.conn.commit()
        return int(cur.lastrowid), True

    def finish_run(self, run_id: int, status: str = "CERRADA") -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, status=? WHERE id=?", (_now(), status, run_id))
        self.conn.commit()

    def last_run_id(self) -> int | None:
        row = self.conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["id"]) if row else None

    def run_fecha(self, run_id: int) -> str:
        return self.conn.execute("SELECT fecha FROM runs WHERE id=?", (run_id,)).fetchone()["fecha"]

    # ---- pacientes (jobs) ----
    def add_job(self, run_id: int, spec: Any) -> int:
        self.conn.execute(
            """INSERT OR IGNORE INTO jobs(run_id,tdoc,documento,ingreso,fecha_cita,email,nombre,
               especialidad,filas,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, spec.tdoc, spec.documento, spec.ingreso, spec.fecha.isoformat(), spec.email,
             spec.nombre, spec.especialidad, json.dumps(spec.filas), "PENDIENTE", _now()))
        self.conn.commit()
        return int(self.conn.execute(
            "SELECT id FROM jobs WHERE run_id=? AND tdoc=? AND documento=? AND ingreso=?",
            (run_id, spec.tdoc, spec.documento, spec.ingreso)).fetchone()["id"])

    def already_sent(self, spec: Any, exclude_run: int) -> bool:
        """¿Este mismo ingreso ya se envió (o quedó incierto) en otra corrida? Evita duplicar historias clínicas."""
        row = self.conn.execute(
            """SELECT 1 FROM jobs WHERE tdoc=? AND documento=? AND ingreso=? AND fecha_cita=? AND run_id<>?
               AND status IN ('ENVIADO','ENVIO_INCIERTO') LIMIT 1""",
            (spec.tdoc, spec.documento, spec.ingreso, spec.fecha.isoformat(), exclude_run)).fetchone()
        return row is not None

    def set_job(self, job_id: int, **fields: Any) -> None:
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
        self.conn.commit()

    def bump_attempts(self, job_id: int) -> int:
        self.conn.execute("UPDATE jobs SET intentos=intentos+1 WHERE id=?", (job_id,))
        self.conn.commit()
        return int(self.conn.execute("SELECT intentos FROM jobs WHERE id=?", (job_id,)).fetchone()["intentos"])

    def jobs_by_status(self, run_id: int, statuses: Iterable[str]) -> list[sqlite3.Row]:
        st = list(statuses)
        q = ",".join("?" for _ in st)
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE run_id=? AND status IN ({q}) ORDER BY id", (run_id, *st)).fetchall()

    def all_jobs(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    def get_job(self, job_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    # ---- documentos ----
    def add_document(self, job_id: int, tipo: str, estado: str, archivo: str | None = None,
                     folio: str | None = None, sha256: str | None = None, paginas: int | None = None,
                     verificado: bool = False, detalle: str = "") -> int:
        cur = self.conn.execute(
            """INSERT INTO documents(job_id,tipo,estado,archivo,folio,sha256,paginas,verificado,detalle,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (job_id, tipo, estado, archivo, folio, sha256, paginas, int(verificado), detalle, _now()))
        self.conn.commit()
        return int(cur.lastrowid)

    def clear_documents(self, job_id: int) -> None:
        self.conn.execute("DELETE FROM documents WHERE job_id=?", (job_id,))
        self.conn.commit()

    def documents(self, job_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM documents WHERE job_id=? ORDER BY id", (job_id,)).fetchall()

    # ---- correos ----
    def add_email(self, job_id: int, destinatario: str, estado: str, adjuntos: list[str],
                  detalle: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO emails(job_id,destinatario,estado,adjuntos,detalle) VALUES(?,?,?,?,?)",
            (job_id, destinatario, estado, json.dumps(adjuntos), detalle))
        self.conn.commit()
        return int(cur.lastrowid)

    def set_email(self, email_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE emails SET {cols} WHERE id=?", (*fields.values(), email_id))
        self.conn.commit()

    # ---- omitidas y eventos ----
    def add_skipped(self, run_id: int, o: Any) -> None:
        self.conn.execute(
            "INSERT INTO skipped(run_id,fila,motivo,tdoc,documento,ingreso,email) VALUES(?,?,?,?,?,?,?)",
            (run_id, o.fila, o.motivo, o.tdoc, o.documento, o.ingreso, o.email))
        self.conn.commit()

    def event(self, run_id: int | None, nivel: str, codigo: str, mensaje: str, job_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(ts,run_id,job_id,nivel,codigo,mensaje) VALUES(?,?,?,?,?,?)",
            (_now(), run_id, job_id, nivel, codigo, mensaje))
        self.conn.commit()

    def table(self, name: str, run_id: int | None = None) -> list[dict]:
        """Filas de una tabla (para exportar). Filtra por corrida cuando aplica."""
        if name == "jobs":
            rows = self.conn.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,))
        elif name == "documents":
            rows = self.conn.execute(
                "SELECT d.*, j.documento, j.ingreso FROM documents d JOIN jobs j ON j.id=d.job_id WHERE j.run_id=?",
                (run_id,))
        elif name == "emails":
            rows = self.conn.execute(
                "SELECT e.*, j.documento, j.ingreso FROM emails e JOIN jobs j ON j.id=e.job_id WHERE j.run_id=?",
                (run_id,))
        elif name == "skipped":
            rows = self.conn.execute("SELECT * FROM skipped WHERE run_id=?", (run_id,))
        elif name == "events":
            rows = self.conn.execute("SELECT * FROM events WHERE run_id=? OR run_id IS NULL", (run_id,))
        else:
            raise KeyError(name)
        return [dict(r) for r in rows.fetchall()]
