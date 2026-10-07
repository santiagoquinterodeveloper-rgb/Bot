"""Verificación de identidad en el PDF y cifrado con contraseña (AES-256)."""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from .text import alnum, norm


@dataclass
class VerifyResult:
    ok: bool
    motivo: str
    paginas: int = 0
    nombre_ok: bool | None = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def has_pdf_markers(path: Path) -> bool:
    """Cabecera %PDF y %%EOF al final: descarta archivos a medio escribir."""
    try:
        size = path.stat().st_size
        if size < 100:
            return False
        with open(path, "rb") as f:
            head = f.read(8)
            f.seek(max(0, size - 1024))
            tail = f.read()
        return head.startswith(b"%PDF") and b"%%EOF" in tail
    except OSError:
        return False


def extract_text(path: Path) -> str:
    reader = PdfReader(str(path))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # página con contenido no extraíble
            parts.append("")
    return "\n".join(parts)


def verify_pdf(path: Path, documento: str, nombre: str = "", requerir_documento: bool = True) -> VerifyResult:
    if not has_pdf_markers(path):
        return VerifyResult(False, "pdf_incompleto_o_corrupto")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            return VerifyResult(False, "pdf_cifrado_de_origen")
        paginas = len(reader.pages)
    except Exception as e:  # noqa: BLE001
        return VerifyResult(False, f"pdf_ilegible:{type(e).__name__}")
    if paginas == 0:
        return VerifyResult(False, "pdf_sin_paginas")

    texto = extract_text(path)
    # quita separadores para tolerar 1.020.226.254 / 1 020 226 254
    plano = re.sub(r"[\s.,\-]", "", texto).upper()
    doc = alnum(documento)
    nombre_ok = None
    if nombre:
        tokens = [t for t in norm(nombre).split() if len(t) > 2]
        if tokens:
            tnorm = norm(texto)
            nombre_ok = sum(t in tnorm for t in tokens) / len(tokens) >= 0.5
    if requerir_documento:
        if not texto.strip():
            return VerifyResult(False, "pdf_sin_texto_extraible", paginas, nombre_ok)
        if doc not in plano:
            return VerifyResult(False, "documento_del_paciente_no_aparece_en_el_pdf", paginas, nombre_ok)
    return VerifyResult(True, "ok", paginas, nombre_ok)


def encrypt_pdf(src: Path, dst: Path, password: str) -> None:
    """Cifra con AES-256 (contraseña de usuario = documento) y comprueba que abre con ella."""
    reader = PdfReader(str(src))
    writer = PdfWriter(clone_from=reader)
    writer.encrypt(user_password=password, owner_password=secrets.token_urlsafe(24), algorithm="AES-256")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        writer.write(f)
    check = PdfReader(str(dst))
    if not check.is_encrypted or int(check.decrypt(password)) == 0 or len(check.pages) != len(reader.pages):
        dst.unlink(missing_ok=True)
        raise RuntimeError("El PDF cifrado no pasó la verificación de apertura con la contraseña")
