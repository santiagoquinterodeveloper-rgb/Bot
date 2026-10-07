"""Lectura de la grilla de 'Ingresos de Pacientes' desde la pantalla. SOLO WINDOWS.

Por qué: en esa grilla los Edit ocultos NO siguen a la fila activa (siempre muestran la última fila), y las celdas
no se exponen como controles. Lo único que muestra el consecutivo de cada fila es lo dibujado, así que se usa el
OCR de Windows (winrt, ya incluido en el sistema; paquetes pip `winrt-*`) y se verifica la fila activa por su
recuadro de foco (borde negro de la celda). Los imports de winrt/PIL son perezosos para no romper otros sistemas.
"""
from __future__ import annotations

import asyncio
import io

from .parsing import Word

_engine = None


def grab(bbox: tuple[int, int, int, int]):
    from PIL import ImageGrab

    return ImageGrab.grab(bbox=bbox)


def _get_engine():
    global _engine
    if _engine is None:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        _engine = OcrEngine.try_create_from_user_profile_languages()
        for tag in ("es-MX", "es-ES", "en-US"):
            if _engine is not None:
                break
            _engine = OcrEngine.try_create_from_language(Language(tag))
        if _engine is None:
            raise RuntimeError("Windows no tiene un idioma de OCR instalado")
    return _engine


async def _recognize(png: bytes, scale: int) -> list[list[Word]]:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await _get_engine().recognize_async(bitmap)
    return [[(w.text, w.bounding_rect.x / scale, w.bounding_rect.y / scale,
              w.bounding_rect.width / scale, w.bounding_rect.height / scale) for w in line.words]
            for line in result.lines]


def ocr_words(img, scale: int = 3) -> list[list[Word]]:
    """Palabras por línea con sus posiciones en px de la imagen original (se amplía para que el OCR lea mejor)."""
    from PIL import Image

    big = img.convert("RGB").resize((img.width * scale, img.height * scale), Image.LANCZOS)
    buf = io.BytesIO()
    big.save(buf, "PNG")
    return asyncio.run(_recognize(buf.getvalue(), scale))


def row_has_focus(img, y: float, half: int = 8, min_dark: int = 14, skip_left: int = 28) -> bool:
    """¿Hay recuadro de foco en la fila centrada en `y`? El recuadro tiene dos bordes verticales negros de ~16 px
    (izquierdo y derecho de la celda, ~170 px de separación). Se ignora la columna selectora de filas (x < skip_left),
    que tiene dos líneas negras en todas las filas; las líneas de la grilla son grises y una letra o dígito no pasa
    de ~11 px de alto."""
    import numpy as np

    a = np.asarray(img.convert("L"), dtype=int)
    band = a[max(0, int(y) - half): int(y) + half + 1, :]
    if not band.size:
        return False
    dark = (band < 60).sum(axis=0)
    dark[:skip_left] = 0
    bordes = np.flatnonzero(dark >= min_dark)
    return len(bordes) >= 2 and int(bordes[-1] - bordes[0]) >= 50
