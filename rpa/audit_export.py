"""Exporta la auditoría de una corrida a Excel (no contiene contenido clínico, solo trazabilidad)."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pandas as pd

from .db import AuditDB


def export_audit(db: AuditDB, run_id: int, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fecha = db.run_fecha(run_id)
    out = out_dir / f"auditoria_{fecha}_run{run_id}.xlsx"
    jobs = db.table("jobs", run_id)
    resumen = Counter(j["status"] for j in jobs)
    skipped = db.table("skipped", run_id)
    resumen_rows = [{"concepto": f"pacientes {k}", "cantidad": v} for k, v in sorted(resumen.items())]
    resumen_rows.append({"concepto": "filas omitidas del Excel", "cantidad": len(skipped)})
    for motivo, n in sorted(Counter(s["motivo"].split(":")[0] for s in skipped).items()):
        resumen_rows.append({"concepto": f"  omitidas: {motivo}", "cantidad": n})
    sheets = {
        "Resumen": resumen_rows,
        "Pacientes": jobs,
        "Documentos": db.table("documents", run_id),
        "Correos": db.table("emails", run_id),
        "Omitidas": skipped,
        "Eventos": db.table("events", run_id),
    }
    for n in range(0, 20):
        try:
            with pd.ExcelWriter(out, engine="openpyxl") as xw:
                for name, rows in sheets.items():
                    pd.DataFrame(rows).to_excel(xw, sheet_name=name, index=False)
            return out
        except PermissionError:
            # El archivo está abierto en Excel: se guarda con otro nombre en vez de perder la auditoría
            out = out_dir / f"auditoria_{fecha}_run{run_id}_{n + 2}.xlsx"
    raise PermissionError(f"No se pudo escribir la auditoría en {out_dir}")