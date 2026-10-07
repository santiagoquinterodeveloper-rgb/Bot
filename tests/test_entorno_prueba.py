import pytest

from rpa.cli import main
from rpa.config import load_config
from rpa.db import AuditDB
from tests.test_orchestrator import FakeDriver, FakeMailer, make_orch


def test_extends_hereda_y_sobrescribe(tmp_path):
    (tmp_path / "base.yaml").write_text(
        "paths: {work_dir: w, db_path: w/a.sqlite}\nmail: {dry_run: true, subject: S}\n"
        "documentos: [{key: HC, listbox: X, prefijo: P, archivo: A}]\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "t.yaml").write_text(
        "extends: ../base.yaml\nentorno: prueba\npaths: {db_path: t.sqlite}\nmail: {dry_run: false}\n", encoding="utf-8")
    c = load_config(sub / "t.yaml")
    assert c.get("entorno") == "prueba" and c.get("mail.dry_run") is False
    assert c.get("mail.subject") == "S" and c.get("paths.work_dir") == "w"      # heredado
    assert c.path("paths.db_path") == sub.resolve() / "t.sqlite"                # relativo al archivo hijo
    assert [d.key for d in c.docs] == ["HC"]


def test_extends_circular(tmp_path):
    (tmp_path / "a.yaml").write_text("extends: b.yaml\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("extends: a.yaml\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(tmp_path / "a.yaml")


def test_db_reset_vacia_todo(cfg, excel_file, pdf_factory):
    db, orch = make_orch(cfg, FakeDriver(pdf_factory), FakeMailer())
    run = orch.prepare(excel_file)
    orch.download(run)
    orch.send(run)
    assert db.all_jobs(run) and db.table("emails", run)
    db.reset()
    for t in ("jobs", "emails", "documents", "skipped", "events"):
        assert db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    assert db.last_run_id() is None


def test_reset_test_env_exige_entorno_prueba(cfg, excel_file, pdf_factory):
    db, orch = make_orch(cfg, FakeDriver(pdf_factory), FakeMailer())
    orch.prepare(excel_file)
    with pytest.raises(RuntimeError):
        orch.reset_test_env()
    assert db.last_run_id() is not None                  # producción: no se borró nada


def test_reset_test_env_borra_bd_y_temporales(cfg, excel_file, pdf_factory):
    cfg.raw["entorno"] = "prueba"
    db, orch = make_orch(cfg, FakeDriver(pdf_factory), FakeMailer())
    run = orch.prepare(excel_file)
    orch.download(run)
    assert orch.staging(run).exists()
    orch.reset_test_env()
    assert db.last_run_id() is None and not (orch.work() / "staging").exists()


def test_cli_reset_db_rechaza_produccion(tmp_path, capsys):
    base = (tmp_path / "c.yaml")
    base.write_text(f"paths: {{work_dir: w, db_path: w/x.sqlite}}\ndocumentos: []\n", encoding="utf-8")
    db = AuditDB(tmp_path / "w" / "x.sqlite")
    db.open_run("2026-10-07", "sha", "x.xlsx")
    db.close()
    with pytest.raises(SystemExit) as e:
        main(["--config", str(base), "reset-db"])
    assert "entorno: prueba" in str(e.value)
    assert AuditDB(tmp_path / "w" / "x.sqlite").last_run_id() is not None


def test_cli_reset_db_en_prueba(tmp_path):
    base = (tmp_path / "c.yaml")
    base.write_text("entorno: prueba\npaths: {work_dir: w, db_path: w/x.sqlite}\ndocumentos: []\n", encoding="utf-8")
    db = AuditDB(tmp_path / "w" / "x.sqlite")
    db.open_run("2026-10-07", "sha", "x.xlsx")
    db.close()
    assert main(["--config", str(base), "reset-db"]) == 0
    assert AuditDB(tmp_path / "w" / "x.sqlite").last_run_id() is None
