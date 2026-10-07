"""Envío por Microsoft Graph (client credentials). Un correo por ingreso, PDFs adjuntos uno a uno."""
from __future__ import annotations

import base64
import html
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable

import requests

from .secrets_store import get_secret

GRAPH = "https://graph.microsoft.com/v1.0"
MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024  # límite de adjunto en línea de sendMail


@dataclass
class SendResult:
    ok: bool
    uncertain: bool = False        # puede haberse enviado: NO reintentar automáticamente
    status_code: int | None = None
    request_id: str | None = None
    error: str = ""
    dry_run: bool = False


class GraphMailer:
    def __init__(self, sender: str, token_provider: Callable[[], str], session: requests.Session | None = None,
                 dry_run: bool = True, max_retries: int = 4, timeout: float = 60, sleep=time.sleep):
        self.sender = sender
        self.token_provider = token_provider
        self.session = session or requests.Session()
        self.dry_run = dry_run
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep

    def send(self, to: str, subject: str, html_body: str, attachments: list[tuple[str, bytes]]) -> SendResult:
        for name, data in attachments:
            if len(data) > MAX_ATTACHMENT_BYTES:
                return SendResult(False, error=f"adjunto_supera_3MB:{name}")
        if self.dry_run:
            return SendResult(True, dry_run=True)

        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": to}}],
                "attachments": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": name,
                        "contentType": "application/pdf",
                        "contentBytes": base64.b64encode(data).decode("ascii"),
                    }
                    for name, data in attachments
                ],
            },
            "saveToSentItems": True,
        }
        url = f"{GRAPH}/users/{self.sender}/sendMail"
        refreshed = False
        attempt = 0
        while True:
            try:
                headers = {"Authorization": f"Bearer {self.token_provider()}"}
                resp = self.session.post(url, headers=headers, json=payload, timeout=self.timeout)
            except requests.exceptions.ConnectTimeout as e:
                # No llegó a conectar: la petición no fue procesada, es seguro reintentar.
                if attempt >= self.max_retries:
                    return SendResult(False, error=f"sin_conexion:{type(e).__name__}")
                attempt += 1
                self._sleep(min(60, 2 ** attempt * 2))
                continue
            except requests.exceptions.RequestException as e:
                return SendResult(False, uncertain=True, error=f"red_incierta:{type(e).__name__}")

            rid = resp.headers.get("request-id") or resp.headers.get("client-request-id")
            code = resp.status_code
            if code == 202:
                return SendResult(True, status_code=code, request_id=rid)
            if code == 401 and not refreshed:
                refreshed = True
                continue
            if code in (429, 503):
                if attempt >= self.max_retries:
                    return SendResult(False, status_code=code, request_id=rid, error=f"limitado:{code}")
                wait = _retry_after(resp) or min(120, 2 ** attempt * 5)
                attempt += 1
                self._sleep(wait)
                continue
            if code >= 500:
                return SendResult(False, uncertain=True, status_code=code, request_id=rid,
                                  error=f"servidor_incierto:{code}")
            return SendResult(False, status_code=code, request_id=rid, error=_graph_error(resp))


def _retry_after(resp: requests.Response) -> float | None:
    v = resp.headers.get("Retry-After")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def _graph_error(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return f"{resp.status_code}:{err.get('code', '')}:{err.get('message', '')}"[:500]
    except Exception:  # noqa: BLE001
        return f"{resp.status_code}:{resp.text[:200]}"


def render_message(cfg, nombre: str, fecha: date) -> tuple[str, str]:
    subject = cfg.get("mail.subject").format(fecha=fecha.strftime("%d/%m/%Y"))
    body = cfg.get("mail.body_html").format(
        nombre=html.escape(nombre.title() if nombre else "paciente"), fecha=fecha.strftime("%d/%m/%Y"))
    return subject, body


def build_mailer(cfg, secret_getter=get_secret) -> GraphMailer:
    sender = cfg.get("mail.sender", "")
    dry = bool(cfg.get("mail.dry_run", True))
    common = dict(max_retries=int(cfg.get("mail.max_retries", 4)), timeout=float(cfg.get("mail.timeout_seconds", 60)))
    if dry:
        return GraphMailer(sender or "dry-run@local", lambda: "", dry_run=True, **common)

    import msal  # import diferido: solo se necesita en modo real

    tenant, client = cfg.get("mail.tenant_id"), cfg.get("mail.client_id")
    if not (tenant and client and sender):
        raise RuntimeError("Faltan mail.tenant_id, mail.client_id o mail.sender en config.yaml")
    if cfg.get("mail.auth", "secret") == "certificate":
        with open(cfg.get("mail.certificate_path"), encoding="utf-8") as f:
            credential = {"private_key": f.read(), "thumbprint": cfg.get("mail.certificate_thumbprint")}
    else:
        credential = secret_getter("graph_client_secret")
        if not credential:
            raise RuntimeError("Falta el secreto: python -m rpa set-secret graph_client_secret")
    app = msal.ConfidentialClientApplication(
        client, authority=f"https://login.microsoftonline.com/{tenant}", client_credential=credential)

    def provider() -> str:
        r = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in r:
            raise RuntimeError(f"No se obtuvo token de Graph: {r.get('error')}: {r.get('error_description')}")
        return r["access_token"]

    return GraphMailer(sender, provider, dry_run=False, **common)
