"""Atributos de seguridad de la cookie de sesión del panel.

La cookie ya era HttpOnly y SameSite=Lax; faltaba `Secure` para que no viaje
en claro sobre HTTP. Se controla por entorno (`SESSION_COOKIE_SECURE`) con
default `false`, porque el PoC corre local sobre http://localhost.
"""

from __future__ import annotations

import pytest
from fastapi import Response

from kavos_lake.adapters.inbound.http import panel as auth_panel


def _set_cookie_header(monkeypatch, value: str | None) -> str:
    if value is None:
        monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    else:
        monkeypatch.setenv("SESSION_COOKIE_SECURE", value)
    response = Response()
    auth_panel._set_session(response, "token-abc")
    return response.headers["set-cookie"]


def test_cookie_siempre_httponly_y_lax(monkeypatch):
    header = _set_cookie_header(monkeypatch, None)
    assert "HttpOnly" in header
    assert "SameSite=lax" in header


def test_cookie_sin_secure_por_default(monkeypatch):
    header = _set_cookie_header(monkeypatch, None)
    assert "Secure" not in header


def test_cookie_secure_cuando_el_entorno_lo_pide(monkeypatch):
    header = _set_cookie_header(monkeypatch, "true")
    assert "Secure" in header
