"""Hosvital simulado con la misma API de pywinauto que usa el driver.

Reproduce la geometría y el comportamiento observados en los volcados reales: pila de diálogos #32770,
botones de ícono 29x29, grillas con Edit ocultos para la fila activa, ListBox cuyo contenido varía,
'Mensaje' para incapacidad inexistente, PDF en la carpeta de reportes y visor Edge. Sirve para probar la
lógica del driver sin Windows. NO prueba cómo reacciona la app real (eso está en la lista de validación).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

from tests.conftest import make_pdf

ORIGIN = (172, 9)


class Rect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class Ctrl:
    _n = 1000

    def __init__(self, app, cls, text="", rect=(0, 0, 0, 0), visible=True, enabled=True, parent=None, pid=1):
        self.app, self.cls, self.text, self.rect = app, cls, text, rect
        self.visible, self.enabled, self.parent, self.pid = visible, enabled, parent, pid
        self.kids: list[Ctrl] = []
        if parent:
            parent.kids.append(self)
        Ctrl._n += 1
        self.handle = Ctrl._n
        self.on_click = self.on_keys = self.on_select = None
        self.items: list[str] = []
        self.sel = -1

    # --- API pywinauto ---
    def class_name(self): return self.cls
    def window_text(self): return self.text
    def rectangle(self): return Rect(*self.rect)
    def is_visible(self): return self.visible
    def is_enabled(self): return self.enabled
    def process_id(self): return self.pid
    def set_focus(self): return self
    def top_level_parent(self):
        c = self
        while c.parent:
            c = c.parent
        return c

    def children(self, **kw):
        return [k for k in self.kids if kw.get("class_name") in (None, k.cls)]

    def descendants(self, **kw):
        out = []
        for k in self.kids:
            if kw.get("class_name") in (None, k.cls):
                out.append(k)
            out.extend(k.descendants(**kw))
        return out

    def click(self, coords=None, **_):
        if coords is not None and getattr(self, "on_xy", None):
            self.on_xy(*coords)                        # clic por mensajes sobre una celda de la grilla
        elif self.enabled and self.on_click:
            self.on_click()

    click_input = click

    def set_edit_text(self, t, *a):
        self.text = t

    def select(self, item=None, *a):
        if self.cls == "Edit":
            return
        self.sel = item if isinstance(item, int) else self.items.index(item)
        if self.on_select:
            self.on_select(self.sel)

    def item_texts(self): return list(self.items)
    def selected_text(self): return self.items[self.sel] if self.sel >= 0 else self.text
    def send_chars(self, chars, **_): self.text = chars

    def send_keystrokes(self, keys, **_):
        silent_ok = self.app.silent_arrows or keys == "{ENTER}"
        if silent_ok and self.on_keys:
            self.on_keys(keys)

    def type_keys(self, keys, **_):
        if self.on_keys:
            self.on_keys(keys)

    def close(self, wait_time=0):
        self.app.close_window(self)


class FakeHosvital:
    def __init__(self, reports_dir: Path, patients: dict, silent_arrows=True, folio_closes=False,
                 running=True, password="clave", expire_after_pdfs: int | None = None, ocr_blank=False,
                 rango_abre_otro=False, folio_slow_polls=0):
        self.folio_slow_polls = folio_slow_polls   # lecturas de la grilla de folios que salen vacías antes de cargar
        self.reports, self.patients = Path(reports_dir), patients
        self.silent_arrows, self.folio_closes, self.running = silent_arrows, folio_closes, running
        self.password, self.expire_after_pdfs, self.pdfs_made = password, expire_after_pdfs, 0
        self.ocr_blank = ocr_blank             # el OCR no devuelve nada (ventana tapada / sin idioma)
        self.rango_abre_otro = rango_abre_otro
        self.rango_dlg, self.rango_header = None, ""
        self.rangos_abiertos: list[int] = []   # consecutivo de la fila activa cada vez que se abre el Rango
        self.ing_grid = None
        self.stack: list[Ctrl] = []
        self.edge: list[Ctrl] = []
        self.main = Ctrl(self, "SWT_Window0", "7J.0 *HOSVITAL*", (175, 35, 1191, 708))
        self.login_edits = [Ctrl(self, "Edit", "", (1057, 553, 1142, 571), parent=self.main),
                            Ctrl(self, "Edit", "", (1057, 583, 1142, 601), parent=self.main)]
        self.login_edits[1].on_keys = self._login
        self.current: dict | None = None      # paciente en pantalla
        self.logins = 0

    # ---------- infraestructura ----------
    def windows(self, class_name=None, visible_only=True):
        if not self.running:
            return []
        wins = list(reversed(self.stack)) + [self.main] + self.edge
        if class_name:
            wins = [w for w in wins if w.cls == class_name]
        return wins

    def close_window(self, w):
        if w in self.edge:
            self.edge.remove(w)

    def expire(self):
        self.stack.clear()
        self.current = None

    def _dlg(self, title, rect):
        d = Ctrl(self, "#32770", title, rect)
        c = Ctrl(self, "SWT_Window0", "", (rect[0] + 3, rect[1] + 26, rect[2] - 3, rect[3] - 3), parent=d)
        self.stack.append(d)
        return d, c

    @staticmethod
    def _abs(origin, rel):
        return (origin[0] + rel[0], origin[1] + rel[1], origin[0] + rel[2], origin[1] + rel[3])

    def _icon(self, parent, origin, rel, click, enabled=True):
        b = Ctrl(self, "Button", "", self._abs(origin, rel), parent=parent, enabled=enabled)
        b.on_click = click
        return b

    def _pop(self, dlg):
        if dlg in self.stack:
            self.stack.remove(dlg)

    def _message(self, text):
        d, c = self._dlg("Mensaje", (500, 250, 900, 400))
        Ctrl(self, "Static", text, (520, 300, 880, 330), parent=c)
        ok = Ctrl(self, "Button", "Aceptar", (700, 360, 780, 390), parent=c)
        ok.on_click = lambda: self._pop(d)

    def _grid(self, parent, rect, rows, n_edits, on_change=None):
        g = Ctrl(self, "SWT_Window0", "", rect, parent=parent)
        g.edits = [Ctrl(self, "Edit", "", (3, 26, 100, 41), visible=False, parent=g) for _ in range(n_edits)]
        g.rows, g.cur = rows, 0

        def refresh():
            row = g.rows[g.cur] if g.rows else [""] * n_edits
            for e, t in zip(g.edits, row):
                e.text = t
            if on_change:
                on_change()

        def keys(k):
            if k == "{DOWN}" and g.cur < len(g.rows) - 1:
                g.cur += 1
            elif k == "{UP}" and g.cur > 0:
                g.cur -= 1
            elif k == "{END}":
                g.cur = len(g.rows) - 1
            refresh()

        g.on_keys, g.refresh = keys, refresh
        refresh()
        return g

    # ---------- PDFs ----------
    def _write_pdf(self, prefix: str, texto: str):
        self.reports.mkdir(parents=True, exist_ok=True)
        name, n = f"{prefix}.pdf", 1
        while (self.reports / name).exists():
            n += 1
            name = f"{prefix}{n}.pdf"
        make_pdf(self.reports / name, texto)
        self.pdfs_made += 1
        e = Ctrl(self, "Chrome_WidgetWin_1", f"{name} - Personal - Microsoft Edge", (0, 0, 800, 600), pid=2)
        self.edge.append(e)
        if self.expire_after_pdfs and self.pdfs_made == self.expire_after_pdfs:
            self.expire()
            self.expire_after_pdfs = None

    # ---------- login -> ciclo -> menús ----------
    def _login(self, keys):
        if keys != "{ENTER}":
            return
        if self.login_edits[1].text != self.password:
            return self._message("Usuario o clave inválidos")
        self.logins += 1
        d, c = self._dlg("Selección Cliclo de Proceso", (432, 188, 933, 532))
        rows = [["2026", str(m), n] for m, n in [(7, "JULIO"), (9, "SEPTIEMBRE"), (10, "OCTUBRE")]]
        self._grid(c, (524, 270, 825, 433), rows, 3).cur = 0
        c.kids[-1].refresh()
        o = (432, 188)
        for i, rel in enumerate([(341, 281, 370, 310), (373, 281, 402, 310), (405, 281, 434, 310), (437, 281, 466, 310)]):
            self._icon(c, o, rel, (lambda dd=d: self._open_menu(dd)) if i == 2 else (lambda: None))

    def _open_menu(self, cycle_dlg):
        self._pop(cycle_dlg)
        d, c = self._dlg("Menu Principal", (172, 9, 1194, 711))
        sis = Ctrl(self, "Button", "", (722, 527, 849, 606), parent=c)
        sis.on_click = self._open_sis

    def _open_sis(self):
        d, c = self._dlg("MENU SISTEMA DE INFORMACION EN SALUD", (172, 9, 1194, 711))
        Ctrl(self, "Button", "", (1013, 75, 1166, 100), parent=c)
        g = Ctrl(self, "Button", "", (1013, 199, 1166, 224), parent=c)
        g.on_click = self._open_gestion

    def _open_gestion(self):
        d, c = self._dlg("Menú Gestión Asistencial", (172, 9, 1194, 711))
        self.gest = d
        self.gest_edit = Ctrl(self, "Edit", "", (639, 480, 739, 500), parent=c, enabled=False)
        self.gest_combo = Ctrl(self, "ComboBox", "", (745, 480, 977, 502), parent=c, enabled=False)
        self.gest_combo.items = ["CEDULA DE CIUDADANIA  ", "TARJETA DE IDENTIDAD    ", "REGISTRO CIVIL"]
        Ctrl(self, "Button", "", (295, 135, 452, 166), parent=c)
        resumen = Ctrl(self, "Button", "", (295, 474, 452, 505), parent=c)

        def enable():
            self.gest_edit.enabled = self.gest_combo.enabled = True
        resumen.on_click = enable
        self._icon(c, (0, 0), (986, 475, 1015, 504), self._buscar_paciente)
        self._icon(c, (0, 0), (1095, 640, 1124, 669), lambda: None)
        self._icon(c, (0, 0), (1127, 640, 1156, 669), lambda: None)

    # ---------- paciente / ingresos ----------
    def _buscar_paciente(self):
        doc = self.gest_edit.text.strip()
        tipo = self.gest_combo.items[self.gest_combo.sel].strip() if self.gest_combo.sel >= 0 else ""
        p = self.patients.get(doc)
        if not p or p["tipo"] != tipo:
            return self._message("El paciente no existe")
        self.current = dict(p, doc=doc)
        o = (360, 116)
        d, c = self._dlg("Ingresos de Pacientes", (360, 116, 1005, 604))
        shown = p.get("shown_doc", doc)
        Ctrl(self, "Edit", shown, self._abs(o, (97, 68, 195, 86)), parent=c)
        Ctrl(self, "Edit", "CC", self._abs(o, (198, 68, 245, 86)), parent=c)
        Ctrl(self, "Edit", p["nombre"], self._abs(o, (248, 68, 616, 86)), parent=c)
        f_ini = Ctrl(self, "Edit", "01/10/2026 00:00:00", self._abs(o, (167, 137, 329, 157)), parent=c)
        f_fin = Ctrl(self, "Edit", "31/10/2026 23:59:59", self._abs(o, (400, 137, 562, 157)), parent=c)
        todos = [["AMBULATORIO", str(k), v["fecha"], ""] for k, v in sorted(p["ingresos"].items())]
        grid = self._ingresos_grid(c, self._abs(o, (18, 170, 593, 410)), list(todos))
        self.ing_grid = grid
        # Real: el ENTER en F. Fin NO refiltra: actúa como el botón por defecto (✓) y abre el Rango con la fila activa.
        f_fin.on_keys = lambda keys: self._abrir_rango(grid) if keys == "{ENTER}" else None
        self._icon(c, o, (536, 440, 565, 469), lambda: None)
        self._icon(c, o, (568, 440, 597, 469), lambda: self._abrir_rango(grid))
        self._icon(c, o, (600, 440, 629, 469), lambda: self._pop(d))

    def _ingresos_grid(self, parent, rect, rows):
        """Grilla de Ingresos como en Hosvital real: los Edit ocultos son HERMANOS posteriores a la grilla y quedan
        obsoletos (siempre muestran la ÚLTIMA fila, no la activa); un clic con coordenadas mueve la fila activa;
        las flechas no la mueven. La fila activa al abrir es la primera."""
        g = Ctrl(self, "SWT_Window0", "", rect, parent=parent)
        g.rows, g.cur, g.rect_abs = rows, 0, rect
        for t in (rows[-1][:3] if rows else ["", "", ""]):
            Ctrl(self, "Edit", t, (3, 26, 100, 41), visible=False, parent=parent)

        def on_xy(x, y):
            i = int((y - 18) // 17)
            if 0 <= i < len(g.rows):
                g.cur = i
        g.on_xy = on_xy
        return g

    def _abrir_rango(self, grid):
        if not grid.rows:
            return self._message("No hay ingresos")
        self.rangos_abiertos.append(int(grid.rows[grid.cur][1]))
        self.ingreso = self.current["ingresos"][int(grid.rows[grid.cur][1])]
        fila = grid.rows[grid.cur]
        if self.rango_abre_otro:                      # Hosvital abre el Rango de otra fila (no la elegida)
            fila = next(r for r in grid.rows if r is not fila)
            self.ingreso = self.current["ingresos"][int(fila[1])]
        self.rango_header = f'Consecutivo de Ingreso {fila[1]} del {fila[2]} Servicio "AMBULATORIO"'
        o = (278, 54)
        d, c = self._dlg("Rango de Impresion HC", (278, 54, 1087, 665))
        self.rango_dlg = d
        lb = Ctrl(self, "ListBox", "", self._abs(o, (19, 79, 261, 406)), parent=c)
        lb.items = ["RANGO DE FECHAS", "RANGO DE FOLIOS", "ENFERMERIA", "CERTIFICADO MEDICO", "INCAPACIDADES  ",
                    "FORMULACION", "ORDENES MEDICAS", "AIEPI"]
        Ctrl(self, "ComboBox", "Del Ingreso", self._abs(o, (20, 50, 119, 72)), parent=c).items = ["Del Ingreso"]
        salida = Ctrl(self, "ComboBox", "EN PANTALLA", self._abs(o, (404, 574, 537, 598)), parent=c)
        salida.items = ["EN PANTALLA", "EN IMPRESORA", "EN PDF"]
        salida.sel = 0
        self._icon(c, o, (682, 562, 711, 591), lambda: None)
        ok = self._icon(c, o, (714, 562, 743, 591), lambda: self._generar(lb, salida, d), enabled=False)
        self._icon(c, o, (746, 562, 775, 591), lambda: self._pop(d))
        lb.on_select = lambda i: setattr(ok, "enabled", True)

    def _generar(self, lb, salida, rango_dlg):
        assert salida.selected_text() == "EN PDF", "el bot debe elegir EN PDF"
        item = lb.items[lb.sel].strip()
        doc, ing = self.current["doc"], self.ingreso
        if item == "RANGO DE FOLIOS":
            self._write_pdf("RHsClxFo", f"Historia clinica CC {doc}")
        elif item == "INCAPACIDADES":
            if ing.get("inc"):
                self._write_pdf("RIncAfi", f"Incapacidad CC {doc}")
            else:
                self._message("Debe seleccionar un Registro de Incapacidad")
        elif item in ("FORMULACION", "ORDENES MEDICAS"):
            key, prefix = ("for", "RForSuAm") if item == "FORMULACION" else ("ord", "ROrmed1")
            self._folio_dialog(ing.get(key, []), prefix, f"{item} CC {doc}", rango_dlg)

    def _folio_dialog(self, folios, prefix, texto, rango_dlg):
        o = (361, 117)
        d, c = self._dlg("Seleccionar Folio", (361, 117, 1005, 603))
        rows = [[""] * 3 + [str(f), "01/10/2026 07:31:29", "", "0"] for f in folios]
        grid = self._grid(c, self._abs(o, (190, 133, 467, 392)), rows, 7)
        empty = ["", "", "", "0", "  /  /       :  :  ", "", "0"]
        if not rows:
            for e, t in zip(grid.edits, empty):
                e.text = t
        elif self.folio_slow_polls:                      # real: la grilla abre vacía y se llena un momento después
            left = [self.folio_slow_polls]
            for e, t in zip(grid.edits, empty):
                e.text = t

            def wt(e=grid.edits[0]):
                if left[0] > 0:
                    left[0] -= 1
                    if left[0] == 0:
                        grid.refresh()
                return e.text
            grid.edits[0].window_text = wt

        def generar():
            if not grid.rows:
                return                                   # grilla vacía: no pasa nada
            f = grid.rows[grid.cur][3]
            self._write_pdf(prefix, f"{texto} folio {f}")
            if self.folio_closes:
                self._pop(d)
        self._icon(c, o, (534, 443, 563, 472), lambda: None)
        self._icon(c, o, (566, 443, 595, 472), generar)
        self._icon(c, o, (599, 443, 628, 472), lambda: self._pop(d))


def _install_vision(app: FakeHosvital, monkeypatch):
    """Simula grab/OCR/foco de rpa.hosvital.vision sobre la grilla de Ingresos del simulador."""
    from rpa.hosvital import vision

    def grab(bbox):
        if app.rango_dlg is not None and app.rango_dlg in app.stack:       # franja del título del Rango
            r = app.rango_dlg.rect
            assert tuple(bbox) == (r[0], r[1] + 20, r[2], r[1] + 100), "captura que no es el título del Rango"
            return types.SimpleNamespace(header=app.rango_header)
        g = app.ing_grid
        assert g is not None and tuple(bbox) == tuple(g.rect_abs), "se pidió una captura que no es la grilla de Ingresos"
        return types.SimpleNamespace(rows=[list(r) for r in g.rows], cur=g.cur)

    def ocr_words(img, scale=3):
        if app.ocr_blank:
            return []
        if hasattr(img, "header"):
            return [[(w, 20 + 40 * i, 38, 38, 12) for i, w in enumerate(img.header.split())]]
        lines = [[("Consecutivo", 152, 6, 78, 8), ("De", 235, 6, 16, 8), ("Ingresos", 256, 6, 57, 10)]]
        for i, r in enumerate(img.rows):
            fecha, hora = r[2].split(" ")
            y = 22 + 17 * i
            lines.append([("AMBULATORIO", 22, y, 89, 9), (r[1], 308, y, 6 * len(r[1]), 9),
                          (fecha, 321, y - 1, 73, 12), (hora, 400, y - 1, 60, 12)])
        return lines

    def row_has_focus(img, y, **_):
        return img.cur == int((y - 18) // 17)

    monkeypatch.setattr(vision, "grab", grab)
    monkeypatch.setattr(vision, "ocr_words", ocr_words)
    monkeypatch.setattr(vision, "row_has_focus", row_has_focus)


def install(app: FakeHosvital, monkeypatch):
    """Sustituye pywinauto por el simulador y las llamadas de sistema del driver."""
    import os
    import subprocess

    _install_vision(app, monkeypatch)

    class Desktop:
        def __init__(self, backend="win32"):
            pass

        def windows(self, **kw):
            return app.windows(class_name=kw.get("class_name"))

    mod = types.ModuleType("pywinauto")
    mod.Desktop = Desktop
    monkeypatch.setitem(sys.modules, "pywinauto", mod)
    for name in [m for m in sys.modules if m.startswith("rpa.hosvital.driver")]:
        monkeypatch.delitem(sys.modules, name)

    def startfile(cmd):
        app.running = True
    monkeypatch.setattr(os, "startfile", startfile, raising=False)

    def fake_run(cmd, **kw):
        app.running = False
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)
