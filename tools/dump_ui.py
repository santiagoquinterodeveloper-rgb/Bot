"""Uso:  python tools/dump_ui.py paso6
Tras ejecutarlo tienes 8 s para hacer clic en la BARRA DE TITULO de la ventana de Hosvital que quieres
volcar. Genera dump_<tag>.txt (controles, posiciones relativas, ítems de combos/listas) y dump_<tag>.png.
OJO: el volcado puede incluir el documento/nombre de un paciente: usa datos de prueba antes de compartirlo."""
import ctypes
import sys
import time

from PIL import ImageGrab
from pywinauto import Desktop
from pywinauto.controls.hwndwrapper import HwndWrapper

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

tag = sys.argv[1] if len(sys.argv) > 1 else "dump"
print("Haz clic en la barra de titulo de la ventana de Hosvital...")
time.sleep(8)

w = HwndWrapper(ctypes.windll.user32.GetForegroundWindow())
base = w.rectangle()
lines = [f"TOP class={w.class_name()!r} title={w.window_text()!r} rect={base}", ""]
for c in w.descendants():
    r = c.rectangle()
    line = (f"class={c.class_name()!r:26} text={c.window_text()[:40]!r:44} "
            f"rel=({r.left-base.left},{r.top-base.top},{r.right-base.left},{r.bottom-base.top}) "
            f"vis={c.is_visible()} en={c.is_enabled()} id={c.control_id()}")
    if c.class_name() in ("ComboBox", "ListBox"):
        try:
            line += f"\n      items={[t.strip() for t in c.item_texts()]}"
        except Exception as e:  # noqa: BLE001
            line += f"\n      items=<error {e}>"
    lines.append(line)
lines += ["", "=== VENTANAS VISIBLES (titulo | clase | pid) ==="]
for t in Desktop(backend="win32").windows(visible_only=True):
    if t.window_text():
        lines.append(f"{t.window_text()[:70]!r} | {t.class_name()} | {t.process_id()}")

open(f"dump_{tag}.txt", "w", encoding="utf-8").write("\n".join(lines))
ImageGrab.grab().save(f"dump_{tag}.png")
print("Listo:", f"dump_{tag}.txt")
