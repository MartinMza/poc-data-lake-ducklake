"""CSRF del panel (hallazgo BAJO #6).

Token sincronizador por sesión: cada POST autenticado exige el token del form
(o el header `X-CSRF-Token` que manda HTMX). Se prueba el camino real, sin
neutralizar la validación.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from kavos_lake.adapters.inbound.http.api import app
from tests.support import PASSWORD, SUPERADMIN_PASSWORD, SUPERADMIN_USERNAME, USERNAME

PANEL = "/auth/panel"
CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post(
            f"{PANEL}/entrar",
            data={"username": USERNAME, "password": PASSWORD},
            follow_redirects=False,
        )
        yield c


def _csrf(client: TestClient) -> str:
    page = client.get(PANEL)
    match = CSRF_RE.search(page.text)
    assert match, "el panel debe exponer el token CSRF en el formulario"
    return match.group(1)


def test_every_authenticated_form_carries_a_csrf_field(client):
    """La página del panel y la de contraseña incluyen el hidden del token."""
    assert CSRF_RE.search(client.get(PANEL).text)
    assert CSRF_RE.search(client.get(f"{PANEL}/password").text)


def test_post_with_valid_form_token_succeeds(client):
    response = client.post(
        f"{PANEL}/keys",
        data={"expires_in_days": 10, "csrf": _csrf(client)},
    )
    assert response.status_code == 200
    assert 'id="nueva-key"' in response.text


def test_post_without_token_is_forbidden(client):
    response = client.post(f"{PANEL}/keys", data={"expires_in_days": 10})
    assert response.status_code == 403
    assert "CSRF" in response.text


def test_post_with_wrong_token_is_forbidden(client):
    response = client.post(
        f"{PANEL}/keys", data={"expires_in_days": 10, "csrf": "token-al-azar"}
    )
    assert response.status_code == 403


def test_post_with_htmx_header_token_succeeds(client):
    """Los botones HTMX no mandan body: el token viaja en el header."""
    response = client.post(
        f"{PANEL}/keys",
        data={"expires_in_days": 10},
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert response.status_code == 200


def test_failed_logout_keeps_the_session(client):
    """Sin token, el POST de salir no borra la sesión."""
    assert client.post(f"{PANEL}/salir", follow_redirects=False).status_code == 403
    assert client.get(PANEL, follow_redirects=False).status_code == 200


def test_logout_with_valid_token_works(client):
    response = client.post(
        f"{PANEL}/salir", data={"csrf": _csrf(client)}, follow_redirects=False
    )
    assert response.status_code == 303
    assert client.get(PANEL, follow_redirects=False).status_code == 303


def test_superadmin_action_without_token_is_forbidden():
    with TestClient(app) as admin:
        admin.post(
            f"{PANEL}/entrar",
            data={"username": SUPERADMIN_USERNAME, "password": SUPERADMIN_PASSWORD},
            follow_redirects=False,
        )
        response = admin.post(
            f"{PANEL}/usuarios", data={"username": "csrf-victima", "role": "user", "mode": "link"}
        )
        assert response.status_code == 403
