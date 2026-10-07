"""Visión de la grilla de Ingresos: OCR de Windows real sobre una imagen sintética (datos ficticios)."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="OCR de Windows")

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from rpa.hosvital import vision  # noqa: E402
from rpa.hosvital.parsing import has_grid_header, parse_grid_rows  # noqa: E402


def _grid_image(filas, foco=None):
    """Imita la grilla de Hosvital: cabecera, filas de 17 px y recuadro de foco negro en la celda del consecutivo."""
    try:
        font = ImageFont.truetype("tahoma.ttf", 11)
    except OSError:
        pytest.skip("no hay fuente Tahoma")
    img = Image.new("RGB", (575, 240), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 575, 18), fill=(235, 235, 235))
    d.text((24, 3), "Tipo de Atención", font=font, fill="black")
    d.text((152, 3), "Consecutivo De Ingresos", font=font, fill="black")
    d.text((324, 3), "Fecha Admisión", font=font, fill="black")
    for i, (num, fecha) in enumerate(filas):
        y = 19 + 17 * i
        d.line((0, y, 0, y + 17), fill="black")             # columna selectora de filas: líneas negras en TODAS las filas
        d.line((20, y, 20, y + 17), fill="black")
        d.line((0, y + 17, 575, y + 17), fill=(190, 190, 190))
        d.text((22, y + 2), "AMBULATORIO", font=font, fill="black")
        d.text((300, y + 2), num, font=font, fill="black")
        d.text((321, y + 2), fecha, font=font, fill="black")
        if foco == i:
            d.rectangle((149, y, 318, y + 16), outline="black")
    return img


def test_ocr_lee_consecutivo_y_fecha_de_cada_fila():
    img = _grid_image([("6", "07/10/2026 07:00:00"), ("17", "07/10/2026 09:20:00")])
    lines = vision.ocr_words(img, 3)
    assert has_grid_header(lines)
    rows = parse_grid_rows(lines)
    assert [(r.numero, r.fecha) for r in rows] == [("6", "07/10/2026"), ("17", "07/10/2026")]
    assert rows[0].y < rows[1].y and abs(rows[1].y - rows[0].y - 17) <= 3      # la y sirve para hacer clic


def test_recuadro_de_foco_solo_en_la_fila_activa():
    img = _grid_image([("6", "07/10/2026 07:00:00"), ("1", "07/10/2026 09:20:00")], foco=1)
    assert vision.row_has_focus(img, 19 + 17 + 8) is True       # fila 2 (con recuadro)
    assert vision.row_has_focus(img, 19 + 8) is False           # fila 1: ni el borde de la grilla ni sus dígitos cuentan
    assert vision.row_has_focus(_grid_image([("1", "07/10/2026 07:00:00")]), 19 + 8) is False   # un '1' no es un borde
