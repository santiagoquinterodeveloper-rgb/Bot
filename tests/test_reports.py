from rpa.reports import ReportsDir
from tests.conftest import make_pdf


def mk(tmp_path):
    return ReportsDir(tmp_path / "rep", tmp_path / "q", {"RHsClxFo": "HC", "ROrmed": "ORD", "RForSuAm": "FOR"},
                      sleep=lambda _: None)


def test_sweep_mueve_a_cuarentena(tmp_path):
    r = mk(tmp_path)
    (tmp_path / "rep").mkdir()
    (tmp_path / "rep" / "citas.xlsx").write_text("x")
    moved = r.sweep()
    assert len(moved) == 1 and moved[0].exists() and not (tmp_path / "rep" / "citas.xlsx").exists()


def test_peek_y_classify(tmp_path):
    r = mk(tmp_path)
    (tmp_path / "rep").mkdir()
    assert r.peek_pdf() is None
    p = make_pdf(tmp_path / "rep" / "RHsClxFo2.pdf", "hola mundo")
    assert r.peek_pdf() == p
    assert r.classify(p) == "HC"
    assert r.classify(tmp_path / "ROrmed12.pdf") == "ORD"
    assert r.classify(tmp_path / "otro.pdf") is None


def test_pdf_incompleto_no_se_toma(tmp_path):
    r = mk(tmp_path)
    (tmp_path / "rep").mkdir()
    p = make_pdf(tmp_path / "rep" / "RHsClxFo.pdf", "x" * 50)
    p.write_bytes(p.read_bytes()[:-30])
    assert r.peek_pdf() is None


def test_take_copia(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", "x")
    dst = ReportsDir.take(p, tmp_path / "out" / "b.pdf", sleep=lambda _: None)
    assert dst.read_bytes() == p.read_bytes()


def test_archivo_que_no_se_pudo_mover_se_ignora(tmp_path, monkeypatch):
    import shutil

    r = mk(tmp_path)
    (tmp_path / "rep").mkdir()
    p = make_pdf(tmp_path / "rep" / "RHsClxFo.pdf", "viejo")
    real_move = shutil.move
    monkeypatch.setattr(shutil, "move", lambda *a, **k: (_ for _ in ()).throw(PermissionError("en uso")))
    assert r.sweep() == []
    assert r.peek_pdf() is None                       # el viejo no se confunde con uno nuevo
    monkeypatch.setattr(shutil, "move", real_move)
    p.write_bytes(make_pdf(tmp_path / "x.pdf", "nuevo contenido distinto").read_bytes())  # Hosvital lo reescribe
    assert r.peek_pdf() == p
