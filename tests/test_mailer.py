import requests

from rpa.mailer import GraphMailer


class FakeResp:
    def __init__(self, code, headers=None, body=None):
        self.status_code, self.headers, self._body = code, headers or {}, body or {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, items):
        self.items, self.calls = list(items), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, headers, json))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def mk(items, **kw):
    s = FakeSession(items)
    return GraphMailer("buzon@x.com", lambda: "tok", session=s, dry_run=False, sleep=lambda _: None, **kw), s


def test_dry_run_no_llama_a_la_red():
    m = GraphMailer("a@b.c", lambda: "", dry_run=True)
    r = m.send("p@x.com", "s", "<p>b</p>", [("a.pdf", b"x")])
    assert r.ok and r.dry_run


def test_envio_ok_estructura_del_payload():
    m, s = mk([FakeResp(202, {"request-id": "rid1"})])
    r = m.send("p@x.com", "Asunto", "<p>hola</p>", [("a.pdf", b"abc")])
    assert r.ok and r.request_id == "rid1"
    url, headers, payload = s.calls[0]
    assert url.endswith("/users/buzon@x.com/sendMail") and headers["Authorization"] == "Bearer tok"
    att = payload["message"]["attachments"][0]
    assert att["name"] == "a.pdf" and att["contentBytes"] == "YWJj"
    assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == "p@x.com"


def test_429_reintenta_y_luego_ok():
    m, s = mk([FakeResp(429, {"Retry-After": "1"}), FakeResp(202)])
    assert m.send("p@x.com", "s", "b", []).ok and len(s.calls) == 2


def test_error_5xx_es_incierto_y_no_reintenta():
    m, s = mk([FakeResp(500)])
    r = m.send("p@x.com", "s", "b", [])
    assert not r.ok and r.uncertain and len(s.calls) == 1


def test_timeout_de_lectura_es_incierto_y_de_conexion_se_reintenta():
    m, _ = mk([requests.exceptions.ReadTimeout()])
    assert m.send("p@x.com", "s", "b", []).uncertain
    m, s = mk([requests.exceptions.ConnectTimeout(), FakeResp(202)])
    assert m.send("p@x.com", "s", "b", []).ok and len(s.calls) == 2


def test_400_error_definitivo_con_mensaje():
    m, _ = mk([FakeResp(403, body={"error": {"code": "ErrorAccessDenied", "message": "nope"}})])
    r = m.send("p@x.com", "s", "b", [])
    assert not r.ok and not r.uncertain and "ErrorAccessDenied" in r.error


def test_adjunto_demasiado_grande():
    m, _ = mk([])
    assert m.send("p@x.com", "s", "b", [("a.pdf", b"x" * (3 * 1024 * 1024 + 1))]).error.startswith("adjunto_supera")
