"""Lógica pura (sin pywinauto) del driver: se puede probar en cualquier sistema operativo."""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..text import norm

DT_RE = re.compile(r"^\d{2}/\d{2}/\d{4}( \d{2}:\d{2}(:\d{2})?)?$")

# (estado, regex sobre el título normalizado). El orden no importa: los títulos son distintos.
STATE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("MENSAJE", re.compile(r"^MENSAJE$")),
    ("FOLIO", re.compile(r"^SELECCIONAR FOLIO$")),
    ("RANGO", re.compile(r"^RANGO DE IMPRESION HC$")),
    ("INGRESOS", re.compile(r"^INGRESOS DE PACIENTES$")),
    ("GESTION", re.compile(r"^MENU GESTION ASISTENCIAL$")),
    ("SIS", re.compile(r"^MENU SISTEMA DE INFORMACION EN SALUD$")),
    ("MENU", re.compile(r"^MENU PRINCIPAL$")),
    ("CICLO", re.compile(r"^SELECCION CL?ICL?O DE PROCESO$")),  # la app escribe "Cliclo"
]
# Si el estado superior es uno de estos en pleno trabajo con un paciente, la sesión se perdió.
ESTADOS_SESION_PERDIDA = {"NO_APP", "LOGIN", "CICLO", "MENU", "SIS"}


def state_from_titles(titles: list[str]) -> str | None:
    """`titles` en orden Z (la más superior primero). Devuelve el estado de la primera conocida."""
    for t in titles:
        n = norm(t)
        for state, pat in STATE_PATTERNS:
            if pat.match(n):
                return state
    return None


@dataclass
class RowInfo:
    numero: str | None     # folio / consecutivo de ingreso
    fecha: str | None      # fecha-hora del registro
    tipo: str | None       # texto (p. ej. AMBULATORIO)

    @property
    def vacia(self) -> bool:
        return self.fecha is None or self.numero in (None, "", "0")


def parse_row(texts: list[str]) -> RowInfo:
    """Interpreta los Edit ocultos de una grilla DataWindow (fila activa).

    Ejemplos: ['', '', '', '71', '01/10/2026 07:31:29', '', '0']  -> numero 71
              ['AMBULATORIO', '8', '01/10/2026 15:20:00', '']      -> numero 8, tipo AMBULATORIO
              ['', '', '', '0', '  /  /       :  :  ', '', '0']    -> vacía
    """
    clean = [t.strip() for t in texts]
    fecha_idx = next((i for i, t in enumerate(clean) if DT_RE.match(t)), None)
    numero = None
    if fecha_idx is not None and fecha_idx > 0 and clean[fecha_idx - 1].isdigit():
        numero = clean[fecha_idx - 1]
    tipo = next((t for t in clean if re.search(r"[A-Za-zÁÉÍÓÚáéíóú]", t)), None)
    return RowInfo(numero=numero, fecha=clean[fecha_idx] if fecha_idx is not None else None, tipo=tipo)


Word = tuple[str, float, float, float, float]   # (texto, x, y, ancho, alto) en px relativos a la grilla

_NUM_RE = re.compile(r"^\d{1,9}$")
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")


@dataclass
class GridRow:
    numero: str            # consecutivo de ingreso
    fecha: str             # dd/mm/aaaa de la fecha de admisión
    hora: str | None       # hh:mm:ss si el OCR la leyó
    x: float               # centro de la celda del consecutivo (px relativos a la grilla): punto de clic
    y: float


def parse_grid_rows(lines: list[list[Word]]) -> list[GridRow]:
    """Filas (consecutivo + fecha) de la grilla de Ingresos a partir de las palabras que lee el OCR, por línea.

    Los Edit ocultos de esta grilla NO siguen a la fila activa (siempre muestran la última), así que la grilla se
    lee de la pantalla. Una fila es un 'dd/mm/aaaa' con un número a su izquierda a la misma altura (el más cercano);
    el resto se ignora. No depende de cómo el OCR agrupe las palabras en líneas."""
    words = [w for line in lines for w in line]

    def yc(w: Word) -> float:
        return w[2] + w[4] / 2

    rows: list[GridRow] = []
    for d in words:
        if not _DATE_RE.match(d[0]):
            continue
        nums = [w for w in words if _NUM_RE.match(w[0]) and w[1] < d[1] and abs(yc(w) - yc(d)) <= 6]
        if not nums:
            continue
        n = max(nums, key=lambda w: w[1])
        horas = [w for w in words if _TIME_RE.match(w[0]) and w[1] > d[1] and abs(yc(w) - yc(d)) <= 6]
        rows.append(GridRow(n[0], d[0], horas[0][0] if horas else None, n[1] + n[3] / 2, yc(n)))
    return sorted(rows, key=lambda r: r.y)


_RANGO_RE = re.compile(r"consecutivo\s+de\s+ingreso\s+(\d{1,9})\s+del\s+(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}:\d{2}))?",
                       re.IGNORECASE)


def parse_rango_header(lines: list[list[Word]]) -> tuple[str, str, str | None] | None:
    """'Consecutivo de Ingreso 6 del 07/10/2026 07:00:00 Servicio ...' (título dibujado en 'Rango de Impresion HC').
    Devuelve (consecutivo, dd/mm/aaaa, hh:mm:ss | None) o None si el OCR no lo leyó."""
    texto = " ".join(w[0] for line in lines for w in line)
    m = _RANGO_RE.search(texto)
    return (m.group(1), m.group(2), m.group(3)) if m else None


def has_grid_header(lines: list[list[Word]]) -> bool:
    """¿El OCR vio la cabecera 'Consecutivo De Ingresos'? Distingue una grilla vacía de un OCR que falló."""
    return any(norm(w[0]).startswith("CONSEC") for words in lines for w in words)


def find_item_index(item_texts: list[str], wanted: str) -> int | None:
    """Índice del ítem por texto (igual, y si no, que empiece por). Nunca por posición."""
    w = norm(wanted)
    exact = [i for i, t in enumerate(item_texts) if norm(t) == w]
    if exact:
        return exact[0]
    starts = [i for i, t in enumerate(item_texts) if norm(t).startswith(w) or w.startswith(norm(t)) and norm(t)]
    return starts[0] if starts else None


def is_icon_size(width: int, height: int, size: int = 29, tol: int = 2) -> bool:
    return abs(width - size) <= tol and abs(height - size) <= tol


def bottom_row_by_left(rects: list[tuple[int, int, int, int]], tol: int = 4) -> list[int]:
    """Índices de los botones de la fila inferior, de izquierda a derecha. rect = (l, t, r, b)."""
    if not rects:
        return []
    max_top = max(r[1] for r in rects)
    idx = [i for i, r in enumerate(rects) if abs(r[1] - max_top) <= tol]
    return sorted(idx, key=lambda i: rects[i][0])


def match_layout(rects: list[tuple[int, int, int, int]], expected: list[int], tol: int = 12) -> int | None:
    """Índice del rectángulo cuyo centro está más cerca del esperado (y dentro de la tolerancia)."""
    ex_cx, ex_cy = (expected[0] + expected[2]) / 2, (expected[1] + expected[3]) / 2
    best, best_d = None, None
    for i, (l, t, r, b) in enumerate(rects):
        dx, dy = abs((l + r) / 2 - ex_cx), abs((t + b) / 2 - ex_cy)
        if dx <= tol and dy <= tol and (best_d is None or dx + dy < best_d):
            best, best_d = i, dx + dy
    return best


def month_matches(texts: list[str], year: int, month: int) -> bool:
    clean = {t.strip() for t in texts}
    return str(year) in clean and str(month) in clean
