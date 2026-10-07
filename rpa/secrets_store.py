"""Credenciales: variable de entorno RPA_<NOMBRE> o Windows Credential Manager (keyring)."""
from __future__ import annotations

import os

SERVICE = "rpa_hosvital"


def get_secret(name: str, username: str = "default") -> str | None:
    env = os.environ.get(f"RPA_{name.upper()}")
    if env:
        return env
    try:
        import keyring

        return keyring.get_password(f"{SERVICE}:{name}", username)
    except Exception:  # noqa: BLE001 - keyring no disponible
        return None


def set_secret(name: str, value: str, username: str = "default") -> None:
    import keyring

    keyring.set_password(f"{SERVICE}:{name}", username, value)
