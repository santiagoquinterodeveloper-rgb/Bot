"""Valida la LECTURA de filas y el MOVIMIENTO con teclas en una grilla (Ingresos, Seleccionar Folio, Ciclo).

Uso: abre la ventana con la grilla (ojalá con 2 o más filas), ejecuta  python tools/probe_grid.py
y en 8 s haz clic en su barra de titulo. Imprime los Edit ocultos (fila activa) antes y después de
mandar {DOWN} en modo silencioso y en modo con foco. Pega la salida para ajustar keys_mode."""
import ctypes
import time

from pywinauto.controls.hwndwrapper import HwndWrapper

print("Haz clic en la barra de titulo de la ventana con la grilla...")
time.sleep(8)
top = HwndWrapper(ctypes.windll.user32.GetForegroundWindow())
print("Ventana:", top.window_text())
cands = [c for c in top.descendants(class_name="SWT_Window0") if c.is_visible()]
cands.sort(key=lambda c: c.rectangle().width() * c.rectangle().height(), reverse=True)
grid = cands[1] if len(cands) > 1 else cands[0]


def row():
    return [e.window_text() for e in grid.children(class_name="Edit")]


print("fila activa  :", row())
grid.send_keystrokes("{DOWN}")
time.sleep(0.5)
print("tras DOWN silencioso:", row())
top.set_focus()
grid.type_keys("{DOWN}", set_foreground=True)
time.sleep(0.5)
print("tras DOWN con foco  :", row())
