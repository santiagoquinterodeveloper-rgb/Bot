from rpa.config import load_config
from rpa.excel_loader import check_email, load_jobs
from tests.conftest import ROOT


def _load(path):
    cfg = load_config(ROOT / "config.yaml")
    return load_jobs(path, cfg.get("filters.estados"), cfg.get("filters.especialidades"), True)


def test_filtros_dedupe_y_motivos(excel_file):
    res = _load(excel_file)
    keys = {j.key for j in res.jobs}
    assert keys == {("CC", "123456789", 1), ("CC", "123456789", 2), ("CC", "987654321", 3)}
    j1 = next(j for j in res.jobs if j.key == ("CC", "123456789", 1))
    assert j1.filas == [2, 3]                      # dos filas del Excel -> una sola tarea
    motivos = {o.fila: o.motivo for o in res.omitidas}
    assert motivos[5].startswith("estado_no_habilitado")
    assert motivos[6].startswith("especialidad_no_habilitada")
    assert motivos[7].startswith("email_dominio_sospechoso")
    assert motivos[8] == "email_vacio"


def test_normalizacion_de_documento_ingreso_fecha_correo(excel_file):
    res = _load(excel_file)
    j = next(j for j in res.jobs if j.documento == "987654321")
    assert (j.tdoc, j.ingreso, str(j.fecha), j.email) == ("CC", 3, "2026-10-01", "mp@outlook.com")


def test_check_email():
    assert check_email("a@b.co") == ("a@b.co", None)
    assert check_email("a@b.com; a@b.com")[0] == "a@b.com"
    assert check_email("a@b.com; c@d.com")[1] == "email_multiples"
    assert check_email("sin-arroba")[1] == "email_invalido"
    assert check_email("x@hotmial.com")[0] is None


def test_faltan_columnas(tmp_path):
    import pandas as pd
    import pytest

    p = tmp_path / "mal.xlsx"
    pd.DataFrame([{"a": 1}]).to_excel(p, index=False)
    with pytest.raises(ValueError):
        _load(p)
