"""Control de la carpeta donde Hosvital deja los PDF (C:\\reportes)."""
from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

from .pdf_tools import has_pdf_markers


class ReportsDir:
    def __init__(self, path: Path, quarantine: Path, prefix_map: dict[str, str],
                 sleep=time.sleep, clock=time.monotonic):
        self.path = Path(path)
        self.quarantine = Path(quarantine)
        self.prefix_map = prefix_map
        self._sleep = sleep
        self._clock = clock
        # Archivos que no se pudieron mover (abiertos en el visor): se ignoran para no confundirlos
        # con un PDF nuevo. Si Hosvital los reescribe, cambia mtime/tamaño y vuelven a contar.
        self.ignored: set[tuple[str, int, int]] = set()

    @staticmethod
    def _sig(p: Path) -> tuple[str, int, int]:
        st = p.stat()
        return (p.name, st.st_mtime_ns, st.st_size)

    def sweep(self) -> list[Path]:
        """Mueve TODO lo que haya en la carpeta a cuarentena (no borra: se purga al final del día)."""
        self.path.mkdir(parents=True, exist_ok=True)
        moved = []
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        for p in list(self.path.iterdir()):
            if not p.is_file():
                continue
            self.quarantine.mkdir(parents=True, exist_ok=True)
            dest = self.quarantine / f"{stamp}_{p.name}"
            try:
                shutil.move(str(p), str(dest))
                moved.append(dest)
            except OSError:
                # archivo abierto por otro programa (p. ej. visor): se reintenta en la próxima barrida
                try:
                    self.ignored.add(self._sig(p))
                except OSError:
                    pass
                continue
        return moved

    def list_pdfs(self) -> list[Path]:
        if not self.path.exists():
            return []
        return sorted(p for p in self.path.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")

    def peek_pdf(self, settle: float = 0.8) -> Path | None:
        """Devuelve un PDF completo y estable (tamaño sin cambios), o None."""
        pdfs = []
        for p in self.list_pdfs():
            try:
                if self._sig(p) not in self.ignored:
                    pdfs.append(p)
            except OSError:
                continue
        if not pdfs:
            return None
        candidate = pdfs[-1]
        try:
            s1 = candidate.stat().st_size
        except OSError:
            return None
        self._sleep(settle)
        try:
            s2 = candidate.stat().st_size
        except OSError:
            return None
        if s1 == s2 and s1 > 0 and has_pdf_markers(candidate):
            return candidate
        return None

    def wait_for_pdf(self, timeout: float, poll: float = 0.25) -> Path | None:
        end = self._clock() + timeout
        while self._clock() < end:
            p = self.peek_pdf()
            if p:
                return p
            self._sleep(poll)
        return None

    def classify(self, path: Path) -> str | None:
        """Tipo de documento según el prefijo del nombre (RHsClxFo*, RIncAfi*, ROrmed*, RForSuAm*)."""
        name = path.name
        # prefijos más largos primero para evitar coincidencias parciales
        for prefix in sorted(self.prefix_map, key=len, reverse=True):
            if name.lower().startswith(prefix.lower()):
                return self.prefix_map[prefix]
        return None

    @staticmethod
    def take(src: Path, dst: Path, retries: int = 20, wait: float = 0.5, sleep=time.sleep) -> Path:
        """Copia el PDF a su destino verificando tamaño. Se deja el original para barrerlo después."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for _ in range(retries):
            try:
                shutil.copy2(src, dst)
                if dst.stat().st_size == src.stat().st_size:
                    return dst
            except OSError as e:
                last = e
            sleep(wait)
        raise OSError(f"No se pudo copiar {src} a {dst}: {last}")
