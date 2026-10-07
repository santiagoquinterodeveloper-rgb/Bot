"""Crea el Excel del entorno de prueba a partir del Excel diario real.

Toma los primeros N ingresos que pasan los filtros y el dedupe del bot (reutiliza `load_jobs`, no reimplementa
reglas), excluye los ya enviados en la BD de producción y reemplaza SOLO la columna de correo por buzones internos
de prueba (reparto aleatorio con semilla fija; todos reciben al menos uno). Luego relee el resultado y lo valida.

Uso (desde la carpeta del proyecto):
    python tools\\crear_excel_prueba.py input\\CITAS-7-10-2026.xlsx
    python tools\\crear_excel_prueba.py input\\CITAS.xlsx --salida prueba\\CITAS_PRUEBA_ENVIO.xlsx --n 15 --semilla 42
"""
import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from rpa.config import load_config  # noqa: E402
from rpa.db import AuditDB  # noqa: E402
from rpa.excel_loader import load_jobs  # noqa: E402

BUZONES = [
    "sistemas4@clinicantioquia.com.co",
    "jefesistemas@clinicantioquia.com.co",
    "practicantesistemas@clinicantioquia.com.co",
    "sistemas@clinicantioquia.com.co",
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("origen", help="Excel diario real")
    ap.add_argument("--salida", default=str(ROOT / "prueba" / "CITAS_PRUEBA_ENVIO.xlsx"))
    ap.add_argument("--n", type=int, default=15, help="cantidad de ingresos")
    ap.add_argument("--semilla", type=int, default=42)
    args = ap.parse_args()
    if args.n < len(BUZONES):
        raise SystemExit(f"--n debe ser al menos {len(BUZONES)} para que cada buzón reciba un correo")

    cfg = load_config(ROOT / "config.yaml")
    filtros = dict(estados=cfg.get("filters.estados"), especialidades=cfg.get("filters.especialidades"),
                   skip_if_bad_email=cfg.get("filters.skip_if_bad_email", True))
    jobs = load_jobs(args.origen, **filtros).jobs

    db_real = cfg.path("paths.db_path")
    if db_real.exists():  # AuditDB crearía el archivo si no existiera
        db = AuditDB(db_real)
        jobs = [j for j in jobs if not db.already_sent(j, exclude_run=-1)]
        db.close()
    jobs.sort(key=lambda j: j.filas[0])
    elegidos = jobs[: args.n]
    if len(elegidos) < args.n:
        raise SystemExit(f"Solo hay {len(elegidos)} ingresos elegibles; se pidieron {args.n}.")

    df = pd.read_excel(args.origen, dtype=str, sheet_name=0)
    sub = df.iloc[[j.filas[0] - 2 for j in elegidos]].copy()  # fila de Excel = índice + 2
    col_correo = next(c for c in df.columns if c.strip().upper() in ("EMAIL", "CORREO", "CORREOELECTRONICO"))
    asignacion = [BUZONES[i % len(BUZONES)] for i in range(args.n)]
    random.Random(args.semilla).shuffle(asignacion)
    sub[col_correo] = asignacion

    salida = Path(args.salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    sub.to_excel(salida, index=False)

    res = load_jobs(salida, **filtros)  # validación: se relee con las reglas del propio bot
    assert not res.omitidas and len(res.jobs) == args.n, f"Validación: {len(res.jobs)} ingresos, omitidas={res.omitidas}"
    assert all(j.email in BUZONES for j in res.jobs), "Validación: hay correos fuera de los buzones de prueba"

    print(f"OK: {salida}  ({args.n} ingresos, todos con filtros válidos y correo de prueba)\n")
    print(f"{'fila':<5}{'documento':<12}{'ingreso':<9}{'especialidad':<30}correo")
    for j in sorted(res.jobs, key=lambda x: x.filas[0]):
        print(f"{j.filas[0]:<5}{'***' + j.documento[-4:]:<12}{j.ingreso:<9}{j.especialidad:<30}{j.email}")


if __name__ == "__main__":
    main()
