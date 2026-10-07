from pathlib import Path

import pandas as pd
import pytest

from rpa.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def make_pdf(path: Path, text: str) -> Path:
    """PDF mínimo válido con texto extraíble."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path


@pytest.fixture
def pdf_factory():
    return make_pdf


@pytest.fixture
def excel_file(tmp_path):
    rows = [
        # OK: urologia atendida
        dict(tdoc="CC", identificacion="123456789", Ingreso="1", Fecha_Cita="2026-10-01 00:00:00.0", Estado="ATENDIDA",
             Especialidad="UROLOGIA", NomprePacietne="PACIENTE UNO", Email="p1@gmail.com", CitFolio="1"),
        # duplicado del mismo ingreso (otra cita) -> una sola tarea
        dict(tdoc="CC", identificacion="123456789", Ingreso="1", Fecha_Cita="2026-10-01 00:00:00.0", Estado="FACTURADA",
             Especialidad="CIRUGIA GENERAL", NomprePacietne="PACIENTE UNO", Email="p1@gmail.com", CitFolio="2"),
        # segundo ingreso del mismo paciente -> otra tarea
        dict(tdoc="CC", identificacion="123456789", Ingreso="2", Fecha_Cita="2026-10-01 00:00:00.0", Estado="ATENDIDA",
             Especialidad="UROLOGIA", NomprePacietne="PACIENTE UNO", Email="p1@gmail.com", CitFolio="3"),
        # estado no habilitado
        dict(tdoc="CC", identificacion="222", Ingreso="1", Fecha_Cita="2026-10-01", Estado="CANCELADA",
             Especialidad="UROLOGIA", NomprePacietne="X", Email="x@gmail.com", CitFolio="1"),
        # especialidad no habilitada (lista blanca)
        dict(tdoc="CC", identificacion="333", Ingreso="1", Fecha_Cita="2026-10-01", Estado="ATENDIDA",
             Especialidad="RADIOLOGIA E IMAGENES DIAGNOSTICAS", NomprePacietne="Y", Email="y@gmail.com", CitFolio="1"),
        # correo con dominio sospechoso
        dict(tdoc="TI", identificacion="444", Ingreso="1", Fecha_Cita="2026-10-01", Estado="ATENDIDA",
             Especialidad="ORTOPEDIA Y TRAUMATOLOGIA", NomprePacietne="Z", Email="z@gmal.com", CitFolio="1"),
        # sin correo
        dict(tdoc="CC", identificacion="555", Ingreso="1", Fecha_Cita="2026-10-01", Estado="ATENDIDA",
             Especialidad="UROLOGIA", NomprePacietne="W", Email="", CitFolio="1"),
        # documento numérico guardado como decimal, estado en minúsculas y con tildes en especialidad
        dict(tdoc="cc", identificacion="987654321.0", Ingreso="3.0", Fecha_Cita="01/10/2026", Estado="atendida",
             Especialidad="Ginecología y Obstetricia", NomprePacietne="Maria Perez", Email="MP@Outlook.com", CitFolio="1"),
    ]
    p = tmp_path / "citas.xlsx"
    pd.DataFrame(rows).to_excel(p, index=False)
    return p


@pytest.fixture
def cfg(tmp_path):
    return load_config(ROOT / "config.yaml", {
        "paths": {"reports_dir": str(tmp_path / "reportes"), "work_dir": str(tmp_path / "work"),
                  "db_path": str(tmp_path / "work" / "rpa.sqlite"), "audit_dir": str(tmp_path / "aud"),
                  "input_excel": str(tmp_path / "citas.xlsx")},
        "mail": {"send_interval_seconds": 0},
    })
