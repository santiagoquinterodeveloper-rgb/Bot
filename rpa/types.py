"""Tipos compartidos entre el orquestador y el driver de Hosvital."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class SessionExpired(Exception):
    """Hosvital volvió al login (o se cerró) en medio de una operación."""


class DriverError(Exception):
    """Fallo recuperable del driver (ventana inesperada, control no encontrado...)."""


class RunAborted(Exception):
    """Demasiados errores seguidos: se detiene la corrida."""


@dataclass
class DocResult:
    tipo: str                     # HC | INC | FOR | ORD
    estado: str                   # DESCARGADO | NO_EXISTE | ERROR
    archivos: list[tuple[Path, str | None]] = field(default_factory=list)  # (ruta, folio)
    detalle: str = ""


@dataclass
class PatientResult:
    estado: str                   # OK | PACIENTE_NO_ENCONTRADO | INGRESO_NO_ENCONTRADO | DOC_DISTINTO
    detalle: str = ""
    docs: list[DocResult] = field(default_factory=list)
