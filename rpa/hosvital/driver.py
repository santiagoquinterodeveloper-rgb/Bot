"""Driver de Hosvital HIS con pywinauto (backend win32). SOLO WINDOWS.

Principios (basados en los volcados de controles reales):
  * Todo se hace por mensajes a los controles (click(), set_edit_text, send_keystrokes), sin mover el mouse.
  * Los botones de ícono no tienen texto: se ubican por tamaño (29x29) y posición relativa a su ventana.
  * El estado se deduce de la pila de ventanas del proceso (título de la ventana superior).
  * Para saber si existe un documento NO se leen grillas: se da check y se observa el resultado
    (PDF nuevo en C:\\reportes / ventana 'Mensaje' / diálogo 'Seleccionar Folio' / login).
  * Las grillas DataWindow exponen la FILA ACTIVA en Edit ocultos (folios y ciclo). La excepción es 'Ingresos de
    Pacientes': ahí los Edit no siguen a la fila activa, y la grilla se lee con OCR (vision.py).

Todo lo marcado con [VALIDAR] depende de comportamiento que solo se puede confirmar en la máquina real
(ver README, "Lista de validación").
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Callable

from pywinauto import Desktop

from ..config import Config, DocType
from ..excel_loader import JobSpec
from ..reports import ReportsDir
from ..secrets_store import get_secret
from ..text import alnum, norm
from ..types import DocResult, DriverError, PatientResult, SessionExpired
from . import vision
from .parsing import (ESTADOS_SESION_PERDIDA, GridRow, RowInfo, bottom_row_by_left, find_item_index,
                      has_grid_header, is_icon_size, match_layout, parse_grid_rows, parse_rango_header,
                      parse_row, state_from_titles)


class HosvitalDriver:
    def __init__(self, cfg: Config, reports: ReportsDir, log: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg, self.reports, self.log, self._sleep = cfg, reports, log, sleep
        self._main_re = re.compile(cfg.get("hosvital.main_title_regex"))
        self.t_dialog = float(cfg.get("hosvital.timeouts.dialog", 20))
        self.t_report = float(cfg.get("hosvital.timeouts.report", 60))
        self.t_launch = float(cfg.get("hosvital.timeouts.launch", 90))
        self._click_mode = cfg.get("hosvital.click_mode", "message")
        self._keys_focus = cfg.get("hosvital.keys_mode", "silent") == "focus"
        self._tol = int(cfg.get("hosvital.layout.tolerance", 12))
        self._max_folios = int(cfg.get("hosvital.max_folios_por_documento", 20))
        self._login_at: float | None = None
        self._fecha: date | None = None
        self._main_handle: int | None = None

    # =====================================================================
    #  Ventanas y estado
    # =====================================================================
    def _all_top(self):
        return Desktop(backend="win32").windows(visible_only=True)

    def _main(self):
        """Ventana principal. Se recuerda su handle: con Hosvital ocupado (p. ej. generando un reporte) las ventanas
        no responden a WM_GETTEXT y su título sale vacío, y por el título solo se daría la app por cerrada."""
        for w in self._all_top():
            try:
                if w.class_name() != "SWT_Window0":
                    continue
                if self._main_handle is not None and w.handle == self._main_handle:
                    return w
                if self._main_re.match(w.window_text()):
                    self._main_handle = w.handle
                    return w
            except Exception:  # noqa: BLE001
                continue
        return None

    def _hos_windows(self, main=None) -> list:
        """Ventanas del proceso de Hosvital (excepto la principal), la más superior primero."""
        main = main or self._main()
        if main is None:
            return []
        pid, out = main.process_id(), []
        for w in self._all_top():
            try:
                if w.process_id() == pid and w.handle != main.handle:
                    out.append(w)
            except Exception:  # noqa: BLE001
                continue
        return out

    def state(self) -> str:
        main = self._main()
        if main is None:
            return "NO_APP"
        busy = False
        for w in self._hos_windows(main):
            titulo = w.window_text()
            st = state_from_titles([titulo])
            if st:
                return st
            if w.class_name() == "#32770":
                if not titulo.strip():  # sin título: Hosvital ocupado (no responde) o ventana construyéndose; no es un Mensaje
                    busy = True
                    continue
                return "MENSAJE"       # diálogo desconocido: se trata como caja de mensaje
        return "OCUPADO" if busy else "LOGIN"

    def _dialog(self, state: str):
        for w in self._hos_windows():
            if state_from_titles([w.window_text()]) == state:
                return w
        raise DriverError(f"No se encontró la ventana {state}; estado actual: {self.state()}")

    def _wait_any(self, states: set[str], timeout: float, raise_if_lost: bool = True) -> str:
        end = time.monotonic() + timeout
        grace = min(float(self.cfg.get("hosvital.lost_grace_seconds", 3.0)), timeout / 2)
        lost_since: float | None = None
        while time.monotonic() < end:
            st = self.state()
            if st in states or st == "MENSAJE":
                return st
            if raise_if_lost and st in ESTADOS_SESION_PERDIDA:
                # Mientras Hosvital construye una ventana puede no responder y la principal "desaparece" un momento
                # (state() = NO_APP/LOGIN): solo es sesión perdida si el estado se mantiene.
                lost_since = lost_since or time.monotonic()
                if time.monotonic() - lost_since >= grace:
                    raise SessionExpired(f"estado {st} mientras se esperaba {sorted(states)}")
            else:
                lost_since = None
            self._sleep(0.25)
        raise DriverError(f"Tiempo agotado esperando {sorted(states)}; estado actual: {self.state()}")

    # =====================================================================
    #  Controles
    # =====================================================================
    @staticmethod
    def _rel(win, ctrl) -> tuple[int, int, int, int]:
        r, o = ctrl.rectangle(), win.rectangle()
        return (r.left - o.left, r.top - o.top, r.right - o.left, r.bottom - o.top)

    def _buttons(self, win) -> list:
        return [b for b in win.descendants(class_name="Button") if b.is_visible()]

    def _icon_buttons(self, win) -> list:
        return [b for b in self._buttons(win)
                if is_icon_size(b.rectangle().width(), b.rectangle().height())]

    def _bottom_icons(self, win) -> list:
        icons = self._icon_buttons(win)
        rects = [(b.rectangle().left, b.rectangle().top, b.rectangle().right, b.rectangle().bottom) for b in icons]
        return [icons[i] for i in bottom_row_by_left(rects)]

    def _check_btn(self, win):
        row = self._bottom_icons(win)
        if len(row) < 2:
            raise DriverError("No se encontró el botón ✓ (fila inferior de íconos)")
        return row[-2]          # orden: ?, ✓, X

    def _cancel_btn(self, win):
        row = self._bottom_icons(win)
        if not row:
            raise DriverError("No se encontró el botón X")
        return row[-1]

    def _by_layout(self, win, name: str):
        expected = self.cfg.get(f"hosvital.layout.{name}")
        btns = self._buttons(win)
        rects = [self._rel(win, b) for b in btns]
        i = match_layout(rects, expected, self._tol)
        if i is None:
            raise DriverError(f"Botón '{name}' no está en la posición esperada {expected}. "
                              f"Posiciones vistas: {rects}. ¿Cambió el diseño de Hosvital?")
        return btns[i]

    def _click(self, ctrl) -> None:
        if self._click_mode == "input":
            ctrl.click_input()
        else:
            ctrl.click()
        self._sleep(0.2)

    def _wait_enabled(self, ctrl, timeout: float = 4.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if ctrl.is_enabled():
                return
            self._sleep(0.15)
        raise DriverError("El botón ✓ sigue deshabilitado (¿no se seleccionó el documento?)")

    def _set_text(self, edit, text: str, secret: bool = False, digits_only: bool = False) -> None:
        edit.set_edit_text(text)
        self._sleep(0.15)
        got = edit.window_text().strip()
        if secret and (got == "" or set(got) <= {"*", "•", "●"}):
            # El campo de contraseña de Hosvital no devuelve su texto (en el volcado sale '' aunque se vean
            # asteriscos): no se puede verificar ni reintentar a ciegas (se duplicaría). Si la clave fuera
            # incorrecta, el login lo avisará con un mensaje.
            return
        if got != text:
            edit.select()                                        # fallback para campos con máscara
            edit.send_chars(re.sub(r"\D", "", text) if digits_only else text)
            self._sleep(0.2)
            got = edit.window_text().strip()
        if got != text:
            shown = "***" if secret else repr(text)
            raise DriverError(f"No se pudo escribir {shown} en el campo; quedó {'***' if secret else repr(got)}")

    # ---- cajas de mensaje -------------------------------------------------
    def _top_window(self):
        wins = self._hos_windows()
        if not wins:
            raise DriverError("No hay ventana de Hosvital abierta")
        return wins[0]

    def _message_text(self) -> str:
        dlg = self._top_window()
        texts = [c.window_text().strip() for c in dlg.descendants(class_name="Static") if c.window_text().strip()]
        return " ".join(texts) or dlg.window_text()

    def _dismiss_message(self) -> None:
        dlg = self._top_window()
        buttons = [b for b in dlg.descendants(class_name="Button") if b.is_visible()]
        pref = [b for b in buttons if norm(b.window_text()).replace("&", "") in ("ACEPTAR", "OK", "SI", "CERRAR")]
        target = (pref or buttons or [None])[0]
        if target is None:
            dlg.close()
        else:
            self._click(target)
        self._sleep(0.3)

    # ---- grillas DataWindow ----------------------------------------------
    def _grid(self, dlg):
        """La grilla es el SWT_Window0 visible más pequeño dentro del contenedor principal."""
        cands = [c for c in dlg.descendants(class_name="SWT_Window0") if c.is_visible()]
        if not cands:
            raise DriverError("No se encontró la grilla de la ventana")
        cands.sort(key=lambda c: c.rectangle().width() * c.rectangle().height(), reverse=True)
        return cands[1] if len(cands) > 1 else cands[0]

    def _row_texts(self, grid) -> list[str]:
        # descendants (no solo hijos directos): entre la grilla y sus Edit puede haber ventanas intermedias
        edits = grid.descendants(class_name="Edit") or grid.children(class_name="Edit")
        if edits:
            return [e.window_text() for e in edits]
        # En Hosvital real los Edit de la fila activa NO son hijos de la grilla: son sus "hermanos" y en el
        # orden de enumeración quedan justo después de ella (volcado del ciclo: SWT_Window0 -> 4 Edit -> Buttons).
        # OJO: top_level_parent() de la grilla es la ventana principal SWT (el diálogo #32770 es una ventana
        # propia, no su ancestro), por eso se sube por parent() hasta el contenedor que sí la enumera.
        items, idx = [], None
        node = grid.parent()
        while node is not None and idx is None:
            items = node.descendants()
            idx = next((i for i, c in enumerate(items) if c.handle == grid.handle), None)
            node = node.parent() if idx is None else None
        out: list[str] = []
        if idx is not None:
            for c in items[idx + 1:]:
                cls = c.class_name()
                if cls == "Edit":
                    out.append(c.window_text())
                elif cls in ("SWT_Window0", "Button", "ComboBox"):
                    break
        return out

    def _grid_debug(self, grid) -> str:
        try:
            return str([(c.class_name(), c.window_text()[:20], c.is_visible()) for c in grid.descendants()][:25])
        except Exception as e:  # noqa: BLE001
            return f"(no se pudo listar: {e})"

    def _row(self, grid) -> RowInfo:
        return parse_row(self._row_texts(grid))

    def _press(self, grid, key: str, force_focus: bool = False) -> None:
        if self._keys_focus or force_focus:
            grid.top_level_parent().set_focus()
            grid.type_keys(key, set_foreground=True)
        else:
            grid.send_keystrokes(key)
        self._sleep(0.3)

    def _next_row(self, grid, key: str = "{DOWN}") -> RowInfo | None:
        """Mueve la fila activa y devuelve su contenido; None si no se movió (extremo de la lista). [VALIDAR]"""
        before = self._row_texts(grid)
        self._press(grid, key)
        after = self._row_texts(grid)
        if after != before:
            return parse_row(after)
        if not self._keys_focus:                     # las teclas silenciosas pueden no llegar a la grilla
            self._press(grid, key, force_focus=True)
            after = self._row_texts(grid)
            if after != before:
                self._keys_focus = True               # a partir de ahora se usa siempre el modo con foco
                self.log("Aviso: las flechas solo funcionan con foco; se usa keys_mode=focus")
                return parse_row(after)
        return None

    def _goto_row(self, grid, numero: str) -> None:
        for key in ("{DOWN}", "{UP}"):
            for _ in range(self._max_folios):
                r = self._next_row(grid, key)
                if r is None:
                    break
                if r.numero == numero:
                    return
        raise DriverError(f"No se pudo ubicar la fila {numero} en la grilla")

    # =====================================================================
    #  Ciclo de vida de la aplicación
    # =====================================================================
    def launch(self) -> None:
        cmd = self.cfg.get("hosvital.launch_command")
        if not cmd:
            raise DriverError("Falta hosvital.launch_command en config.yaml")
        self.log("Lanzando Hosvital...")
        os.startfile(cmd)  # noqa: S606 (Windows)
        end = time.monotonic() + self.t_launch
        while time.monotonic() < end:
            if self.state() == "LOGIN":
                self._sleep(1.0)
                return
            self._sleep(0.5)
        raise DriverError("Hosvital no mostró la pantalla de login a tiempo")

    def restart(self) -> None:
        main = self._main()
        if main is not None:
            subprocess.run(["taskkill", "/PID", str(main.process_id()), "/T", "/F"], capture_output=True)
            end = time.monotonic() + 20
            while self._main() is not None and time.monotonic() < end:
                self._sleep(0.5)
        self._login_at = None
        self._main_handle = None

    def login(self) -> None:
        user = self.cfg.get("hosvital.username")
        pwd = get_secret("hosvital_password")
        if not user or not pwd:
            raise DriverError("Falta hosvital.username o el secreto: python -m rpa set-secret hosvital_password")
        main = self._main()
        edits = sorted((e for e in main.descendants(class_name="Edit")
                        if e.is_visible() and e.rectangle().width() > 60), key=lambda e: e.rectangle().top)
        if len(edits) < 2:
            raise DriverError("No se encontraron los campos Usuario/Contraseña [VALIDAR con tools/dump_ui.py]")
        self.log("Login...")
        self._set_text(edits[0], str(user))
        self._set_text(edits[1], pwd, secret=True)
        self._submit_login(main, edits[1])
        st = self._wait_any({"CICLO"}, self.t_dialog + 20, raise_if_lost=False)
        if st == "MENSAJE":
            msg = self._message_text()
            self._dismiss_message()
            raise DriverError(f"El login fue rechazado: {msg}")
        self._login_at = time.monotonic()

    def _submit_login(self, main, pwd_edit) -> None:
        """Da clic al botón de continuar del login (antes solo se enviaba ENTER, que no llegaba)."""
        buttons = [b for b in main.descendants(class_name="Button") if b.is_visible()]
        self.log("Botones en el login: " + str(
            [(b.window_text(), b.rectangle().width(), b.rectangle().height()) for b in buttons]))
        names = ("ACEPTAR", "INGRESAR", "CONTINUAR", "ENTRAR", "OK", "SIGUIENTE")
        pref = [b for b in buttons if norm(b.window_text()).replace("&", "") in names]
        target = pref[0] if pref else None
        if target is None:
            # El botón ► del login no tiene texto: está pegado a la derecha del campo de contraseña
            # (volcado real: campo 885..970 x 573..593, botón 977..994 x 571..595). Los íconos de 29x29
            # de abajo a la izquierda son otra cosa (usuario / salir) y NO se deben pulsar.
            er = pwd_edit.rectangle()
            near = [b for b in buttons
                    if 0 <= b.rectangle().left - er.right <= 40
                    and abs(b.rectangle().top - er.top) <= 15
                    and not is_icon_size(b.rectangle().width(), b.rectangle().height())]
            near.sort(key=lambda b: b.rectangle().left)
            target = near[0] if near else None
        if target is not None:
            self._click(target)
        else:                                            # último recurso: ENTER con foco real
            main.set_focus()
            pwd_edit.type_keys("{ENTER}", set_foreground=True)

    def pick_cycle(self) -> None:
        """Selecciona el último ciclo de la grilla (sin validar fecha): Hosvital permite descargar documentos
        de cualquier fecha una vez entrado al último ciclo."""
        dlg = self._dialog("CICLO")
        grid = self._grid(dlg)
        self._press(grid, "{END}")
        if not self._row_texts(grid):
            self._press(grid, "{END}", force_focus=True)
        if not self._row_texts(grid):
            raise DriverError(f"No se pudo leer la fila del ciclo. Contenido de la grilla: {self._grid_debug(grid)}")
        self._click(self._check_btn(dlg))
        st = self._wait_any({"MENU"}, self.t_dialog, raise_if_lost=False)
        if st == "MENSAJE":
            msg = self._message_text()
            self._dismiss_message()
            raise DriverError(f"No se pudo seleccionar el ciclo: {msg}")

    def open_sis(self) -> None:
        self._click(self._by_layout(self._dialog("MENU"), "menu_principal_sis"))
        self._wait_any({"SIS"}, self.t_dialog, raise_if_lost=False)

    def open_gestion(self) -> None:
        self._click(self._by_layout(self._dialog("SIS"), "menu_sis_gestion"))
        self._wait_any({"GESTION"}, self.t_dialog, raise_if_lost=False)

    def _close_top(self) -> None:
        st = self.state()
        if st == "MENSAJE":
            self._dismiss_message()
        elif st in ("INGRESOS", "RANGO", "FOLIO"):
            self._click(self._cancel_btn(self._dialog(st)))
            self._sleep(0.3)

    def _session_too_old(self) -> bool:
        limit = float(self.cfg.get("hosvital.session_max_minutes", 330)) * 60
        return bool(self.cfg.get("hosvital.proactive_restart", True)) and self._login_at is not None \
            and time.monotonic() - self._login_at > limit

    def ensure_ready(self, fecha: date) -> None:
        """Deja Hosvital en 'Menú Gestión Asistencial', listo para el siguiente paciente."""
        self._fecha = fecha
        if self.state() == "GESTION" and self._session_too_old():
            self.log("Reinicio planificado de Hosvital (sesión cerca de las 6 h)")
            self.restart()
        for _ in range(20):
            st = self.state()
            if st == "GESTION":
                return
            if st == "NO_APP":
                self.launch()
            elif st == "LOGIN":
                self.login()
            elif st == "CICLO":
                self.pick_cycle()
            elif st == "MENU":
                self.open_sis()
            elif st == "SIS":
                self.open_gestion()
            else:                                  # MENSAJE / INGRESOS / RANGO / FOLIO
                self._close_top()
            self._sleep(0.3)
        raise DriverError("No se logró llegar a Gestión Asistencial")

    def recover(self) -> None:
        self._close_pdf_viewers()
        for _ in range(8):
            st = self.state()
            if st in ("GESTION", "NO_APP", "LOGIN", "CICLO", "MENU", "SIS"):
                return
            self._close_top()

    # =====================================================================
    #  Visor de PDF (Edge)
    # =====================================================================
    def _close_pdf_viewers(self, wait: float = 0.0) -> int:
        closed, end = 0, time.monotonic() + wait
        while True:
            for w in Desktop(backend="win32").windows(class_name="Chrome_WidgetWin_1", visible_only=True):
                try:
                    t = w.window_text().lower()
                    if ".pdf" in t and "edge" in t:
                        w.close()
                        closed += 1
                except Exception:  # noqa: BLE001
                    continue
            if closed or time.monotonic() >= end:
                return closed
            self._sleep(0.3)

    # =====================================================================
    #  Flujo de un paciente
    # =====================================================================
    def download_patient(self, job: JobSpec, outdir: Path) -> PatientResult:
        outdir.mkdir(parents=True, exist_ok=True)
        self._close_pdf_viewers()
        self.reports.sweep()

        gest = self._dialog("GESTION")
        self._click(self._by_layout(gest, "gestion_resumen"))                       # "Resumen de Historia"
        self._fill_patient(gest, job)
        st = self._wait_any({"INGRESOS"}, self.t_dialog)
        if st == "MENSAJE":
            msg = self._message_text()
            self._dismiss_message()
            return PatientResult("PACIENTE_NO_ENCONTRADO", msg)

        ing = self._dialog("INGRESOS")
        problema = self._verify_patient_header(ing, job)
        if problema:
            self._click(self._cancel_btn(ing))
            return PatientResult("DOC_DISTINTO", problema)
        if not self._select_ingreso(ing, job):
            self._click(self._cancel_btn(self._dialog("INGRESOS")))
            return PatientResult("INGRESO_NO_ENCONTRADO",
                                 f"No apareció el consecutivo {job.ingreso} del {job.fecha:%d/%m/%Y}")
        self._click(self._check_btn(ing))
        st = self._wait_any({"RANGO"}, self.t_dialog)
        if st == "MENSAJE":
            msg = self._message_text()
            self._dismiss_message()
            return PatientResult("INGRESO_NO_ENCONTRADO", f"Hosvital no abrió el rango de impresión: {msg}")
        problema = self._verify_rango(job)
        if problema:                                       # se abrió el Rango de OTRO ingreso: no se descarga nada
            self._click(self._cancel_btn(self._dialog("RANGO")))
            return PatientResult("INGRESO_NO_ENCONTRADO", problema)

        docs = [self._download_doc(d, outdir) for d in self.cfg.docs]
        self._close_pdf_viewers()
        self._back_to_gestion()
        return PatientResult("OK", docs=docs)

    def _back_to_gestion(self) -> None:
        for _ in range(8):
            if self.state() == "GESTION":
                return
            self._close_top()
        raise DriverError("No se pudo volver al menú de Gestión Asistencial")

    def _fill_patient(self, gest, job: JobSpec) -> None:
        combos = [c for c in gest.descendants(class_name="ComboBox") if c.is_visible()]
        if not combos:
            raise DriverError("No apareció el formulario del paciente tras 'Resumen de Historia'")
        combo = combos[0]
        end = time.monotonic() + 6
        while not combo.is_enabled() and time.monotonic() < end:
            self._sleep(0.2)
        cb = combo.rectangle()
        edits = [e for e in gest.descendants(class_name="Edit")
                 if e.is_visible() and e.rectangle().right <= cb.left + 10
                 and abs((e.rectangle().top + e.rectangle().bottom) - (cb.top + cb.bottom)) < 20]
        if not edits:
            raise DriverError("No se encontró el campo ID del paciente")
        wanted = self.cfg.get("tipos_documento", {}).get(job.tdoc)
        if not wanted:
            raise DriverError(f"Tipo de documento '{job.tdoc}' sin equivalencia en config.yaml (tipos_documento)")
        items = combo.item_texts()
        idx = find_item_index(items, wanted)
        if idx is None:
            raise DriverError(f"El combo de tipo de documento no tiene '{wanted}'. Ítems: {[i.strip() for i in items]}")
        combo.select(idx)
        self._set_text(edits[0], job.documento)
        check = [b for b in self._icon_buttons(gest)
                 if b.rectangle().left >= cb.right - 5
                 and abs((b.rectangle().top + b.rectangle().bottom) / 2 - (cb.top + cb.bottom) / 2) < 20]
        if not check:
            raise DriverError("No se encontró el botón ✓ junto al ID del paciente")
        self._click(check[0])

    def _verify_patient_header(self, ing, job: JobSpec) -> str | None:
        """Compara el documento que muestra Hosvital con el del Excel. Devuelve el problema o None."""
        header = sorted(
            ((self._rel(ing, e), e) for e in ing.descendants(class_name="Edit") if e.is_visible()),
            key=lambda x: x[0][0])
        row = [e for (r, e) in header if 55 <= r[1] <= 100 and (r[2] - r[0]) > 40]
        if not row:
            raise DriverError("No se pudo leer el documento del paciente en 'Ingresos de Pacientes' [VALIDAR]")
        mostrado = alnum(row[0].window_text())
        if mostrado != alnum(job.documento):
            return f"Hosvital mostró el documento {mostrado} y el Excel trae {alnum(job.documento)}"
        return None

    def _grid_rows(self, grid) -> tuple[list[GridRow], bool]:
        """Filas de la grilla leídas de la pantalla (OCR) y si se vio la cabecera (grilla legible)."""
        r = grid.rectangle()
        end, intento = time.monotonic() + self.t_dialog, 0
        while True:
            intento += 1
            img = vision.grab((r.left, r.top, r.right, r.bottom))
            lines: list = []
            for scale in (3, 4):                              # si a 3x no sale ninguna fila, se reintenta ampliando más
                lines = vision.ocr_words(img, scale)
                rows = parse_grid_rows(lines)
                if rows:
                    return rows, True
            if has_grid_header(lines):                        # se ve la cabecera y ninguna fila: grilla vacía
                return [], True
            if time.monotonic() >= end:                       # la ventana puede tardar en pintarse: se reintenta
                break
            self._sleep(0.5)
        shot = self.reports.quarantine.parent / "screens" / f"grilla_ingresos_{int(time.time())}.png"
        try:
            shot.parent.mkdir(parents=True, exist_ok=True)
            img.save(shot)
            self.log(f"OCR sin resultado tras {intento} intentos; captura de la grilla: {shot}")
        except Exception:  # noqa: BLE001
            pass
        return [], False

    def _verify_rango(self, job: JobSpec) -> str | None:
        """El título de 'Rango de Impresion HC' dice 'Consecutivo de Ingreso N del dd/mm/aaaa hh:mm:ss' (texto
        dibujado, no es un control): se lee con OCR y debe ser el ingreso y el día del Excel. None = correcto."""
        rango = self._dialog("RANGO")
        r = rango.rectangle()
        franja = (r.left, r.top + 20, r.right, r.top + 100)
        end, titulo = time.monotonic() + self.t_dialog, None
        while titulo is None:
            img = vision.grab(franja)
            for scale in (2, 3):
                titulo = parse_rango_header(vision.ocr_words(img, scale))
                if titulo:
                    break
            if titulo is None:
                if time.monotonic() >= end:
                    raise DriverError("No se pudo leer el título 'Consecutivo de Ingreso' del Rango de Impresión (OCR)")
                self._sleep(0.5)
        numero, fecha, _hora = titulo
        if numero != str(job.ingreso) or fecha != f"{job.fecha:%d/%m/%Y}":
            return (f"El Rango de Impresión abrió el ingreso {numero} del {fecha}; el Excel pide el "
                    f"{job.ingreso} del {job.fecha:%d/%m/%Y}")
        return None

    def _focus_rows(self, grid, rows: list[GridRow]):
        """Captura la grilla y devuelve (imagen, [¿tiene el recuadro de foco?] por fila)."""
        r = grid.rectangle()
        img = vision.grab((r.left, r.top, r.right, r.bottom))
        return img, [vision.row_has_focus(img, o.y) for o in rows]

    def _select_ingreso(self, ing, job: JobSpec) -> bool:
        """Deja activa la fila cuyo consecutivo y fecha = los del Excel. False si no está (o es ambigua).

        Los Edit ocultos de esta grilla no siguen a la fila activa y las flechas no la mueven; el ENTER en las
        fechas actúa como ✓ y no filtra. Por eso se lee la grilla con OCR y se activa la fila con un clic en su
        celda de consecutivo (por mensajes), comprobando después el recuadro de foco."""
        grid = self._grid(ing)
        objetivo, fecha = str(job.ingreso), f"{job.fecha:%d/%m/%Y}"
        for lectura in range(3):                              # el OCR puede omitir una fila: se confirma antes de rendirse
            rows, legible = self._grid_rows(grid)
            if not rows and not legible:
                raise DriverError("No se pudo leer la grilla de ingresos con OCR (¿ventana tapada o sin idioma de OCR?)")
            self.log(f"Ingresos en pantalla: {[(r.numero, r.fecha) for r in rows]}")
            cand = [r for r in rows if r.numero == objetivo and r.fecha == fecha]
            if len(cand) == 1:
                break
            self._sleep(0.5)
        else:
            return False                                      # no está (o se repite: ambiguo)
        fila = cand[0]
        i = rows.index(fila)
        for _ in range(3):
            grid.click(coords=(int(fila.x), int(fila.y)))
            self._sleep(0.5)                                  # el recuadro de foco tarda en repintarse
            img, focos = self._focus_rows(grid, rows)
            if focos[i] and sum(focos) == 1:                  # el foco está en la fila elegida y en ninguna otra
                return True
        try:
            shot = self.reports.quarantine.parent / "screens" / f"grilla_foco_{int(time.time())}.png"
            shot.parent.mkdir(parents=True, exist_ok=True)
            img.save(shot)
        except Exception:  # noqa: BLE001
            shot = None
        raise DriverError(f"No se pudo activar la fila del ingreso {objetivo}: foco por fila "
                          f"{[(r.numero, f) for r, f in zip(rows, focos)]} (captura: {shot})")

    # ---------------------------------------------------------------------
    #  Un documento (ventana 'Rango de Impresion HC')
    # ---------------------------------------------------------------------
    def _select_listbox(self, rango, texto: str) -> None:
        lbs = [c for c in rango.descendants(class_name="ListBox") if c.is_visible()]
        if not lbs:
            raise DriverError("No se encontró la lista de documentos (ListBox)")
        lb = lbs[0]
        items = lb.item_texts()
        idx = find_item_index(items, texto)            # por TEXTO: la lista cambia según el paciente
        if idx is None:
            raise DriverError(f"La lista de documentos no tiene '{texto}'. Ítems: {[i.strip() for i in items]}")
        lb.select(idx)
        self._sleep(0.3)

    def _set_output_pdf(self, rango) -> None:
        for c in rango.descendants(class_name="ComboBox"):
            if not c.is_visible():
                continue
            items = c.item_texts()
            idx = find_item_index(items, "EN PDF")
            if idx is not None:
                if "PDF" not in norm(c.selected_text()):
                    c.select(idx)
                return
        raise DriverError("No se encontró el combo 'Salida del reporte' con la opción EN PDF")

    def _wait_result(self, timeout: float, allow_folio: bool) -> tuple[str, object]:
        end = time.monotonic() + timeout
        grace = min(float(self.cfg.get("hosvital.lost_grace_seconds", 3.0)), timeout / 2)
        lost_since: float | None = None
        while time.monotonic() < end:
            pdf = self.reports.peek_pdf()
            if pdf:
                return "PDF", pdf
            st = self.state()
            if st == "MENSAJE":
                return "MENSAJE", self._message_text()
            if st == "FOLIO" and allow_folio:
                return "FOLIO", None
            if st in ESTADOS_SESION_PERDIDA:                   # solo si se mantiene (ver _wait_any)
                lost_since = lost_since or time.monotonic()
                if time.monotonic() - lost_since >= grace:
                    raise SessionExpired(f"estado {st} esperando el resultado del reporte")
            else:
                lost_since = None
            self._sleep(0.3)
        return "TIMEOUT", None

    def _take_pdf(self, doc: DocType, pdf: Path, dest: Path) -> str | None:
        """Copia el PDF a su carpeta, cierra el visor y limpia C:\\reportes. Devuelve un error o None.

        El nombre del archivo no se valida contra `doc.prefijo`: Hosvital no siempre nombra el PDF
        con el prefijo esperado (p. ej. ÓRDENES MÉDICAS generando 'RRslInter*.pdf' según la especialidad)
        y en ese momento ya es, sin ambigüedad, el único PDF nuevo y estable tras pedir este documento.
        """
        if self.reports.classify(pdf) != doc.key:
            self.log(f"Aviso: '{pdf.name}' no tiene el prefijo esperado ({doc.prefijo}) para {doc.key}; se toma igual")
        ReportsDir.take(pdf, dest)
        self._close_pdf_viewers(float(self.cfg.get("hosvital.edge_wait_seconds", 5)))
        self.reports.sweep()
        return None

    def _download_doc(self, doc: DocType, outdir: Path) -> DocResult:
        rango = self._dialog("RANGO")
        self._close_pdf_viewers()
        self.reports.sweep()
        self._select_listbox(rango, doc.listbox)
        self._set_output_pdf(rango)
        check = self._check_btn(rango)
        self._wait_enabled(check)
        self._click(check)
        if doc.folio_dialog:
            return self._folio_flow(doc, outdir)

        kind, payload = self._wait_result(self.t_report, allow_folio=False)
        if kind == "PDF":
            dest = outdir / f"{doc.key}.pdf"
            err = self._take_pdf(doc, payload, dest)  # type: ignore[arg-type]
            return DocResult(doc.key, "ERROR", detalle=err) if err else DocResult(doc.key, "DESCARGADO", [(dest, None)])
        if kind == "MENSAJE":
            texto = str(payload)
            self._dismiss_message()
            if doc.key == "INC" and "INCAPACIDAD" in norm(texto):
                return DocResult(doc.key, "NO_EXISTE", detalle=texto)       # "Debe seleccionar un Registro de Incapacidad"
            return DocResult(doc.key, "ERROR", detalle=f"mensaje de Hosvital: {texto}")
        return DocResult(doc.key, "ERROR", detalle="Hosvital no generó el PDF a tiempo")

    def _wait_first_row(self, grid) -> RowInfo:
        """'Seleccionar Folio' abre con la grilla vacía y la llena un momento después: se espera a que aparezca la
        primera fila antes de darla por vacía (si de verdad no hay registros, se agota `folio_load_seconds`)."""
        end = time.monotonic() + float(self.cfg.get("hosvital.folio_load_seconds", 6))
        while True:
            first = self._row(grid)
            if not first.vacia or time.monotonic() >= end:
                return first
            self._sleep(0.3)

    def _folio_flow(self, doc: DocType, outdir: Path) -> DocResult:
        """Formulación / Órdenes: abre 'Seleccionar Folio'; se descarga cada fila de la grilla."""
        kind, payload = self._wait_result(self.t_dialog, allow_folio=True)
        if kind == "MENSAJE":
            texto = str(payload)
            self._dismiss_message()
            return DocResult(doc.key, "ERROR", detalle=f"mensaje de Hosvital: {texto}")
        if kind == "PDF":                                   # Hosvital descargó directo, sin diálogo
            dest = outdir / f"{doc.key}.pdf"
            err = self._take_pdf(doc, payload, dest)        # type: ignore[arg-type]
            return DocResult(doc.key, "ERROR", detalle=err) if err else DocResult(doc.key, "DESCARGADO", [(dest, None)])
        if kind != "FOLIO":
            return DocResult(doc.key, "ERROR", detalle="No se abrió 'Seleccionar Folio'")

        dlg = self._dialog("FOLIO")
        grid = self._grid(dlg)
        first = self._wait_first_row(grid)
        if first.vacia:
            self._click(self._cancel_btn(dlg))              # grilla vacía: no hay documento, se cierra
            self._sleep(0.3)
            return DocResult(doc.key, "NO_EXISTE", detalle="sin registros en Seleccionar Folio")

        rows = [first]
        while len(rows) < self._max_folios:
            nxt = self._next_row(grid, "{DOWN}")
            if nxt is None or nxt.vacia:
                break
            rows.append(nxt)
        for _ in range(len(rows) - 1):                      # volver a la primera fila
            self._next_row(grid, "{UP}")

        archivos: list[tuple[Path, str | None]] = []
        for idx, row in enumerate(rows):
            if self.state() != "FOLIO":                     # [VALIDAR] el diálogo puede cerrarse tras cada PDF
                self._click(self._check_btn(self._dialog("RANGO")))
                self._wait_any({"FOLIO"}, self.t_dialog)
                dlg = self._dialog("FOLIO")
                grid = self._grid(dlg)
                for _ in range(idx):
                    self._next_row(grid, "{DOWN}")
            dlg = self._dialog("FOLIO")
            grid = self._grid(dlg)
            if self._row(grid).numero != row.numero:
                self._goto_row(grid, row.numero or "")
            self.reports.sweep()
            self._click(self._check_btn(dlg))
            k2, p2 = self._wait_result(self.t_report, allow_folio=False)
            if k2 != "PDF":
                if k2 == "MENSAJE":
                    self._dismiss_message()
                return DocResult(doc.key, "ERROR", detalle=f"folio {row.numero}: {p2 if k2 == 'MENSAJE' else 'sin PDF a tiempo'}")
            dest = outdir / f"{doc.key}_{row.numero}.pdf"
            err = self._take_pdf(doc, p2, dest)             # type: ignore[arg-type]
            if err:
                return DocResult(doc.key, "ERROR", detalle=f"folio {row.numero}: {err}")
            archivos.append((dest, row.numero))

        if self.state() == "FOLIO":
            self._click(self._cancel_btn(self._dialog("FOLIO")))
            self._sleep(0.3)
        return DocResult(doc.key, "DESCARGADO", archivos)