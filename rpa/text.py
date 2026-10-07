"""Utilidades de normalización de texto."""
from __future__ import annotations

import re
import unicodedata


def norm(s: object) -> str:
    """MAYÚSCULAS, sin tildes, espacios colapsados."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().upper()


def alnum(s: object) -> str:
    """Solo letras y dígitos, en mayúsculas (para comparar documentos)."""
    return re.sub(r"[^0-9A-Za-z]", "", "" if s is None else str(s)).upper()
