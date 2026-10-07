"""Prueba el driver real contra el Hosvital simulado (misma API de pywinauto)."""
import importlib

import pandas as pd
import pytest

from rpa.db import AuditDB
from rpa.orchestrator import Orchestrator
from rpa.reports import ReportsDir
from tests.fake_hosvital import FakeHosvital, install
from tests.test_orchestrator import FakeMailer

PAC = {
    "123456789": {"nombre": "PACIENTE UNO", "tipo": "CEDULA DE CIUDADANIA", "ingresos": {
        1: {"fecha": "01/10/2026 14:40:00", "inc": False, "for": [71], "ord": [5, 6]},
        2: {"fecha": "01/10/2026 18:00:00", "inc": True, "for": [], "ord": []}}},
    "987654321": {"nombre": "MARIA PEREZ", "tipo": "CEDULA DE CIUDADANIA", "ingresos": {
        3: {"fecha": "01/10/2026 09:00:00", "inc": False, "for": [], "ord": [9]}}},
}


@pytest.fixture
def setup(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("RPA_HOSVITAL_PASSWORD", "clave")
    cfg.raw["hosvital"].update({"launch_command": "x", "username": "u"})
    cfg.raw["hosvital"]["timeouts"] = {"dialog": 1.0, "report": 1.0, "launch": 2.0}
    cfg.raw["hosvital"]["edge_wait_seconds"] = 0.2
    cfg.raw["hosvital"]["folio_load_seconds"] = 0.3

    def build(**kw):
        app = FakeHosvital(tmp_path / "reportes", kw.pop("patients", PAC), **kw)
        install(app, monkeypatch)
        drv_mod = importlib.import_module("rpa.hosvital.driver")
        reports = ReportsDir(cfg.path("paths.reports_dir"), cfg.path("paths.work_dir") / "quarantine",
                             cfg.prefix_map(), sleep=lambda _: None)
        driver = drv_mod.HosvitalDriver(cfg, reports, log=lambda m: None, sleep=lambda _: None)
        db = AuditDB(cfg.path("paths.db_path"))
        mailer = FakeMailer()
        orch = Orchestrator(cfg, db, driver, mailer, sleep=lambda _: None, log=lambda m: None)
        return app, db, orch, mailer
    return build


def job(db, run, doc, ing):
    return next(j for j in db.all_jobs(run) if j["documento"] == doc and j["ingreso"] == ing)


def docs(db, row):
    out = {}
    for d in db.documents(row["id"]):
        out.setdefault(d["tipo"], []).append((d["estado"], d["folio"]))
    return out


@pytest.mark.parametrize("folio_closes", [False, True])
def test_flujo_completo(setup, excel_file, folio_closes):
    app, db, orch, mailer = setup(folio_closes=folio_closes)
    run = orch.prepare(excel_file)
    orch.download(run)

    j1 = job(db, run, "123456789", 1)
    assert j1["status"] == "DESCARGADO", j1["detalle"]
    d1 = docs(db, j1)
    assert d1["HC"] == [("DESCARGADO", None)]
    assert d1["INC"] == [("NO_EXISTE", None)]                       # mensaje "Debe seleccionar..."
    assert d1["FOR"] == [("DESCARGADO", "71")]
    assert d1["ORD"] == [("DESCARGADO", "5"), ("DESCARGADO", "6")]  # varias filas: una por una

    j2 = job(db, run, "123456789", 2)                               # segundo ingreso: se elige por consecutivo
    assert j2["status"] == "DESCARGADO"
    d2 = docs(db, j2)
    assert d2["INC"] == [("DESCARGADO", None)]
    assert d2["FOR"] == [("NO_EXISTE", None)] and d2["ORD"] == [("NO_EXISTE", None)]   # grilla vacía

    assert job(db, run, "987654321", 3)["status"] == "DESCARGADO"
    assert not app.edge                                              # no quedan visores abiertos
    assert not list(orch.cfg.path("paths.reports_dir").glob("*.pdf"))  # carpeta de reportes limpia
    assert app.logins == 0 or app.logins == 1

    orch.send(run)
    assert all(j["status"] == "ENVIADO" for j in db.all_jobs(run))
    adj = next(m for m in mailer.sent if m[0] == "p1@gmail.com")[2]
    assert "Historia_clinica.pdf" in adj


def test_login_ciclo_y_menus_desde_cero(setup, excel_file):
    app, db, orch, _ = setup(running=False)
    run = orch.prepare(excel_file)
    orch.download(run)
    assert app.logins == 1
    assert job(db, run, "123456789", 1)["status"] == "DESCARGADO"


def test_flechas_silenciosas_no_llegan_se_usa_foco(setup, excel_file):
    app, db, orch, _ = setup(silent_arrows=False)
    run = orch.prepare(excel_file)
    orch.download(run)
    j = job(db, run, "123456789", 2)                                 # exige moverse con las flechas
    assert j["status"] == "DESCARGADO", j["detalle"]
    assert docs(db, job(db, run, "123456789", 1))["ORD"] == [("DESCARGADO", "5"), ("DESCARGADO", "6")]


def test_sesion_expira_a_mitad_y_se_reintenta(setup, excel_file):
    app, db, orch, _ = setup(expire_after_pdfs=2)
    run = orch.prepare(excel_file)
    orch.download(run)
    assert app.logins >= 2                                           # volvió a iniciar sesión
    assert all(j["status"] == "DESCARGADO" for j in db.all_jobs(run))


def test_documento_distinto_al_del_excel(setup, excel_file):
    pac = {"123456789": PAC["123456789"],
           "987654321": dict(PAC["987654321"], shown_doc="000111222")}
    app, db, orch, mailer = setup(patients=pac)
    run = orch.prepare(excel_file)
    orch.download(run)
    assert job(db, run, "987654321", 3)["status"] == "DOC_DISTINTO"   # Hosvital mostró otra persona: se aborta


def test_paciente_inexistente(setup, excel_file):
    app, db, orch, _ = setup(patients={"987654321": PAC["987654321"]})
    run = orch.prepare(excel_file)
    orch.download(run)
    j = job(db, run, "123456789", 1)
    assert j["status"] == "PACIENTE_NO_ENCONTRADO" and "no existe" in j["detalle"]


# ---- varios ingresos el mismo día (la grilla real: Edit ocultos obsoletos, ENTER = ✓, clic mueve la fila) ----
DOS = {"555000111": {"nombre": "PACIENTE DOS", "tipo": "CEDULA DE CIUDADANIA", "ingresos": {
    6: {"fecha": "07/10/2026 07:00:00", "inc": False, "for": [], "ord": []},
    7: {"fecha": "07/10/2026 09:20:00", "inc": False, "for": [], "ord": []}}}}


def excel_de(tmp_path, filas):
    base = dict(tdoc="CC", identificacion="555000111", Fecha_Cita="2026-10-07 00:00:00.0", Estado="ATENDIDA",
                Especialidad="UROLOGIA", NomprePacietne="PACIENTE DOS", Email="p2@gmail.com", CitFolio="1")
    p = tmp_path / "dos.xlsx"
    pd.DataFrame([{**base, **f} for f in filas]).to_excel(p, index=False)
    return p


@pytest.mark.parametrize("pedido", [6, 7])
def test_dos_ingresos_el_mismo_dia_se_elige_el_pedido(setup, tmp_path, pedido):
    app, db, orch, _ = setup(patients=DOS)
    run = orch.prepare(excel_de(tmp_path, [dict(Ingreso=str(pedido))]))
    orch.download(run)
    assert job(db, run, "555000111", pedido)["status"] == "DESCARGADO"
    assert app.rangos_abiertos == [pedido]            # el Rango se abrió desde la fila pedida y solo una vez


def test_dos_ingresos_dan_dos_registros_y_dos_correos(setup, tmp_path):
    app, db, orch, mailer = setup(patients=DOS)
    run = orch.prepare(excel_de(tmp_path, [dict(Ingreso="6"), dict(Ingreso="7", Especialidad="CIRUGIA GENERAL")]))
    orch.download(run)
    assert [j["status"] for j in db.all_jobs(run)] == ["DESCARGADO", "DESCARGADO"]
    assert app.rangos_abiertos == [6, 7]
    orch.send(run)
    assert [m[0] for m in mailer.sent] == ["p2@gmail.com", "p2@gmail.com"]      # un correo por ingreso


def test_consecutivo_inexistente_con_dos_filas(setup, tmp_path):
    app, db, orch, _ = setup(patients=DOS)
    run = orch.prepare(excel_de(tmp_path, [dict(Ingreso="9")]))
    orch.download(run)
    assert job(db, run, "555000111", 9)["status"] == "INGRESO_NO_ENCONTRADO"
    assert app.rangos_abiertos == []                  # no se descargó el de otra fila por error


def test_mismo_consecutivo_en_otra_fecha_no_se_acepta(setup, tmp_path):
    app, db, orch, _ = setup(patients=DOS)
    run = orch.prepare(excel_de(tmp_path, [dict(Ingreso="6", Fecha_Cita="2026-10-08")]))
    orch.download(run)
    assert job(db, run, "555000111", 6)["status"] == "INGRESO_NO_ENCONTRADO"
    assert app.rangos_abiertos == []


def test_un_solo_ingreso_sigue_igual(setup, excel_file):
    app, db, orch, _ = setup()
    run = orch.prepare(excel_file)
    orch.download(run)
    assert job(db, run, "987654321", 3)["status"] == "DESCARGADO"
    assert app.rangos_abiertos.count(3) == 1          # una sola apertura del Rango para el paciente de un ingreso


def test_si_el_rango_abre_otro_ingreso_no_se_descarga_nada(setup, tmp_path):
    app, db, orch, _ = setup(patients=DOS, rango_abre_otro=True)       # el título del Rango dice otro consecutivo
    run = orch.prepare(excel_de(tmp_path, [dict(Ingreso="6")]))
    orch.download(run)
    j = job(db, run, "555000111", 6)
    assert j["status"] == "INGRESO_NO_ENCONTRADO" and "abrió el ingreso 7" in j["detalle"]
    assert db.documents(j["id"]) == [] and app.pdfs_made == 0          # no se generó ningún PDF del ingreso equivocado


def test_hosvital_ocupado_no_se_confunde_con_sesion_perdida_ni_con_mensaje(setup):
    from datetime import date
    app, db, orch, _ = setup()
    orch.driver.ensure_ready(date(2026, 10, 1))
    assert orch.driver.state() == "GESTION"
    titulos = [(w, w.text) for w in [app.main, *app.stack]]
    for w, _ in titulos:                     # con la app ocupada ninguna ventana responde: todos los títulos vacíos
        w.text = ""
    assert orch.driver.state() == "OCUPADO"  # antes: NO_APP (sesión perdida) o MENSAJE
    for w, t in titulos:
        w.text = t
    assert orch.driver.state() == "GESTION"


def test_ocr_ilegible_no_adivina(setup, tmp_path):
    app, db, orch, _ = setup(patients=DOS, ocr_blank=True)
    run = orch.prepare(excel_de(tmp_path, [dict(Ingreso="6")]))
    orch.download(run)
    j = job(db, run, "555000111", 6)
    assert j["status"] == "ERROR_DESCARGA" and "OCR" in j["detalle"]
    assert app.rangos_abiertos == []


def test_ingreso_inexistente(setup, excel_file):
    pac = {"123456789": {**PAC["123456789"], "ingresos": {1: PAC["123456789"]["ingresos"][1]}},
           "987654321": PAC["987654321"]}
    app, db, orch, _ = setup(patients=pac)
    run = orch.prepare(excel_file)
    orch.download(run)
    assert job(db, run, "123456789", 2)["status"] == "INGRESO_NO_ENCONTRADO"
    assert job(db, run, "123456789", 1)["status"] == "DESCARGADO"


def test_seleccionar_folio_carga_lenta_no_se_da_por_vacio(setup, excel_file):
    app, db, orch, _ = setup(folio_slow_polls=4)             # la grilla sale vacía en las primeras lecturas
    run = orch.prepare(excel_file)
    orch.download(run)
    d1 = docs(db, job(db, run, "123456789", 1))
    assert d1["FOR"] == [("DESCARGADO", "71")]
    assert d1["ORD"] == [("DESCARGADO", "5"), ("DESCARGADO", "6")]
