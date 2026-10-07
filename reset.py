"""Reinicia a PENDIENTE los pacientes marcados ERROR_DESCARGA para que la corrida los reintente.

Uso (desde la carpeta del proyecto, con el entorno virtual activo):
    python reset.py
"""
import sqlite3

c = sqlite3.connect("work/rpa.sqlite")
c.execute("UPDATE jobs SET status='PENDIENTE', intentos=0, detalle='' WHERE status='ERROR_DESCARGA'")
c.commit()
print("Reiniciados:", c.total_changes)