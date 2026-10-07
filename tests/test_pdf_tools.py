from pypdf import PdfReader

from rpa.pdf_tools import encrypt_pdf, has_pdf_markers, verify_pdf
from tests.conftest import make_pdf


def test_verify_ok_y_con_separadores(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", "Paciente MARIA PEREZ CC 1.020.226.254")
    assert verify_pdf(p, "1020226254", "Maria Perez").ok
    assert verify_pdf(p, "1020226254", "Maria Perez").nombre_ok is True


def test_verify_documento_ajeno(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", "Paciente JUAN CC 999")
    r = verify_pdf(p, "1020226254")
    assert not r.ok and r.motivo == "documento_del_paciente_no_aparece_en_el_pdf"


def test_verify_pdf_a_medias(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", "x" * 50)
    trunc = tmp_path / "t.pdf"
    trunc.write_bytes(p.read_bytes()[:-20])
    assert not has_pdf_markers(trunc)
    assert verify_pdf(trunc, "1").motivo == "pdf_incompleto_o_corrupto"


def test_encrypt_roundtrip(tmp_path):
    src = make_pdf(tmp_path / "a.pdf", "Doc 123456789")
    dst = tmp_path / "enc" / "a.pdf"
    encrypt_pdf(src, dst, "123456789")
    r = PdfReader(str(dst))
    assert r.is_encrypted
    assert int(r.decrypt("incorrecta")) == 0
    assert int(r.decrypt("123456789")) != 0
    assert "123456789" in r.pages[0].extract_text()
