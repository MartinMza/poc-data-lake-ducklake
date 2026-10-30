"""Rate-limiting de login de punta a punta (REST y panel).

Con el default de 5 intentos, el sexto para el mismo usuario devuelve 429 en vez
de seguir aceptando intentos de fuerza bruta.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dulse.adapters.inbound.http.api import app
from tests.support import PASSWORD, USERNAME


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_token_endpoint_bloquea_tras_demasiados_fallos(client):
    for _ in range(5):
        r = client.post("/auth/token", json={"username": USERNAME, "password": "mal"})
        assert r.status_code == 401
    blocked = client.post("/auth/token", json={"username": USERNAME, "password": "mal"})
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    # Aun con la password correcta, el usuario queda bloqueado mientras dure.
    good = client.post("/auth/token", json={"username": USERNAME, "password": PASSWORD})
    assert good.status_code == 429


def test_login_exitoso_no_queda_bloqueado(client):
    for _ in range(4):  # por debajo del umbral
        client.post("/auth/token", json={"username": USERNAME, "password": "mal"})
    ok = client.post("/auth/token", json={"username": USERNAME, "password": PASSWORD})
    assert ok.status_code == 200


def test_panel_login_bloquea_tras_demasiados_fallos(client):
    for _ in range(5):
        r = client.post(
            "/auth/panel/entrar",
            data={"username": USERNAME, "password": "mal"},
            follow_redirects=False,
        )
        assert r.status_code == 401
    blocked = client.post(
        "/auth/panel/entrar",
        data={"username": USERNAME, "password": "mal"},
        follow_redirects=False,
    )
    assert blocked.status_code == 429
    assert "Demasiados intentos" in blocked.text
