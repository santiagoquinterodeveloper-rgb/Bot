"""Lectura del Excel de citas: reglas (estado/especialidad), validación de correo y deduplicación.

Importante: los filtros de Excel solo ocultan filas; pandas las lee igual. Por eso las reglas se
aplican aquí y cada fila omitida queda registrada con su motivo.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .text import alnum, norm

COLUMNAS = {
    "tdoc": ["TDOC", "TIPODOC", "TIPODOCUMENTO"],
    "documento": ["IDENTIFICACION", "DOCUMENTO", "NUMERODOCUMENTO"],
    "ingreso": ["INGRESO"],
    "fecha": ["FECHA_CITA", "FECHACITA"],
    "estado": ["ESTADO"],
    "especialidad": ["ESPECIALIDAD"],
    "email": ["EMAIL", "CORREO", "CORREOELECTRONICO"],
    "nombre": ["NOMPREPACIETNE", "NOMBREPACIENTE", "NOMBRE_PACIENTE", "PACIENTE"],
    "folio": ["CITFOLIO"],
}
REQUERIDAS = ["tdoc", "documento", "ingreso", "fecha", "estado", "especialidad"]

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
DOMINIOS_SOSPECHOSOS = {
    "gmal.com": "gmail.com", "gmial.com": "gmail.com", "gamil.com": "gmail.com",
    "gmail.con": "gmail.com", "gmail.co": "gmail.com", "gmai.com": "gmail.com",
    "hotmial.com": "hotmail.com", "hotmai.com": "hotmail.com", "hotmail.con": "hotmail.com",
    "outlok.com": "outlook.com", "outlook.con": "outlook.com", "yaho.com": "yahoo.com",
    "yahoo.con": "yahoo.com",
}


@dataclass
class JobSpec:
    tdoc: str
    documento: str
    ingreso: int
    fecha: date
    email: str | None
    nombre: str
    especialidad: str
    estado: str
    filas: list[int] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.tdoc, self.documento, self.ingreso)


@dataclass
class Omitida:
    fila: int
    motivo: str
    tdoc: str = ""
    documento: str = ""
    ingreso: str = ""
    email: str = ""


@dataclass
class ExcelResult:
    jobs: list[JobSpec]
    omitidas: list[Omitida]
    advertencias: list[str]
    sha256: str


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_email(raw: object) -> tuple[str | None, str | None]:
    """Devuelve (correo, error). Nunca corrige dominios: enviar a una dirección adivinada es un riesgo."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None, "email_vacio"
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return None, "email_vacio"
    partes = [p for p in re.split(r"[;,\s]+", text) if p]
    unicos = {p.lower() for p in partes}
    if len(unicos) > 1:
        return None, "email_multiples"
    email = partes[0].lower()
    if not EMAIL_RE.match(email):
        return None, "email_invalido"
    dominio = email.rsplit("@", 1)[1]
    if dominio in DOMINIOS_SOSPECHOSOS:
        return None, f"email_dominio_sospechoso(¿{DOMINIOS_SOSPECHOSOS[dominio]}?)"
    return email, None


def _parse_fecha(v: object) -> date | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat"):
        return None
    base = s.split(".")[0].replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(base, fmt).date()
        except ValueError:
            continue
    return None


def _clean_text(v: object) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def _clean_doc(v: object) -> str:
    s = _clean_text(v)
    if re.fullmatch(r"\d+\.0", s):  # número guardado como decimal en Excel
        s = s[:-2]
    return alnum(s)


def _map_columns(df: pd.DataFrame) -> dict[str, str]:
    by_norm = {re.sub(r"\s+", "", norm(c)): c for c in df.columns}
    out: dict[str, str] = {}
    for canon, aliases in COLUMNAS.items():
        for a in aliases:
            if a in by_norm:
                out[canon] = by_norm[a]
                break
    missing = [c for c in REQUERIDAS if c not in out]
    if missing:
        raise ValueError(
            f"Al Excel le faltan columnas requeridas: {missing}. Columnas encontradas: {list(df.columns)}"
        )
    return out


def load_jobs(path: str | Path, estados: list[str], especialidades: list[str],
              skip_if_bad_email: bool = True) -> ExcelResult:
    path = Path(path)
    df = pd.read_excel(path, dtype=str, sheet_name=0)
    cols = _map_columns(df)
    estados_ok = {norm(e) for e in estados}
    esp_ok = {norm(e) for e in especialidades}

    jobs: dict[tuple, JobSpec] = {}
    omitidas: list[Omitida] = []
    avisos: list[str] = []

    for idx, row in df.iterrows():
        fila = int(idx) + 2  # fila real en Excel (encabezado = 1)
        get = lambda k: row[cols[k]] if k in cols else None  # noqa: E731
        tdoc = norm(_clean_text(get("tdoc")))
        documento = _clean_doc(get("documento"))
        ingreso_raw = _clean_text(get("ingreso"))
        email_raw = _clean_text(get("email"))
        base = dict(fila=fila, tdoc=tdoc, documento=documento, ingreso=ingreso_raw, email=email_raw)

        if not documento or not tdoc:
            omitidas.append(Omitida(motivo="sin_documento_o_tipo", **base))
            continue
        estado = norm(_clean_text(get("estado")))
        if estado not in estados_ok:
            omitidas.append(Omitida(motivo=f"estado_no_habilitado:{estado or 'VACIO'}", **base))
            continue
        esp = norm(_clean_text(get("especialidad")))
        if esp not in esp_ok:
            omitidas.append(Omitida(motivo=f"especialidad_no_habilitada:{esp or 'VACIA'}", **base))
            continue
        try:
            ingreso = int(float(ingreso_raw))
        except ValueError:
            omitidas.append(Omitida(motivo="ingreso_invalido", **base))
            continue
        fecha = _parse_fecha(get("fecha"))
        if fecha is None:
            omitidas.append(Omitida(motivo="fecha_invalida", **base))
            continue
        email, err = check_email(email_raw)
        if err and skip_if_bad_email:
            omitidas.append(Omitida(motivo=err, **base))
            continue

        spec = JobSpec(tdoc=tdoc, documento=documento, ingreso=ingreso, fecha=fecha, email=email,
                       nombre=_clean_text(get("nombre")), especialidad=esp, estado=estado, filas=[fila])
        if spec.key in jobs:
            prev = jobs[spec.key]
            prev.filas.append(fila)
            if email and prev.email and email != prev.email:
                avisos.append(f"Filas {prev.filas}: correos distintos para el mismo ingreso; se usa {prev.email}")
            if email and not prev.email:
                prev.email = email
            if fecha != prev.fecha:
                avisos.append(f"Filas {prev.filas}: fechas distintas para el mismo ingreso; se usa {prev.fecha}")
        else:
            jobs[spec.key] = spec

    return ExcelResult(list(jobs.values()), omitidas, avisos, sha256_file(path))
