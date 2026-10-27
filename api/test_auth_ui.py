"""Tests del panel de credenciales (`/ui/api/*`), por su interfaz HTTP.

Cubre lo que el usuario pidió y lo que la UI no puede garantizar sola: token de
setup de un solo uso, sesión por cookie, usuarios sólo para el superAdmin y los
límites de 5 keys y 180 días aplicados en el servidor.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import auth, auth_db, test_support
from api.main import app
from api.test_support import PASSWORD, SUPERADMIN_PASSWORD, SUPERADMIN_USERNAME, USERNAME


@pytest.fixture()
def client():
    test_support.seed()
    auth._API_KEY_CACHE.clear()
    with TestClient(app) as test_client:
        yield test_client


def _no_superadmin() -> str:
    """Deja la base sin superAdmin y devuelve un token de setup válido."""
    with auth_db.connection() as conn:
        conn.execute("TRUNCATE users, api_keys, sessions, setup_tokens CASCADE")
        conn.commit()
        return auth_db.issue_setup_token(conn)


def _login(client, username=USERNAME, password=PASSWORD):
    response = client.post("/ui/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response


def test_state_reports_a_configured_instance(client):
    state = client.get("/ui/api/state").json()
    assert state["needs_setup"] is False
    assert state["authenticated"] is False
    assert state["max_keys"] == 5
    assert state["max_expiry_days"] == 180


def test_state_asks_for_setup_without_superadmin(client):
    _no_superadmin()
    state = client.get("/ui/api/state").json()
    assert state["needs_setup"] is True


def test_setup_creates_the_superadmin_with_the_token(client):
    token = _no_superadmin()
    response = client.post(
        "/ui/api/setup",
        json={"token": token, "username": "jefa", "password": "password-largo"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["role"] == "superadmin"
    # el token queda quemado
    second = client.post(
        "/ui/api/setup",
        json={"token": token, "username": "otro", "password": "password-largo"},
    )
    assert second.status_code == 409
    assert client.get("/ui/api/state").json()["authenticated"] is True


def test_setup_rejects_a_wrong_token(client):
    _no_superadmin()
    response = client.post(
        "/ui/api/setup",
        json={"token": "inventado", "username": "jefa", "password": "password-largo"},
    )
    assert response.status_code == 400


def test_private_endpoints_require_a_session(client):
    for path in ("/ui/api/me", "/ui/api/keys", "/ui/api/users"):
        assert client.get(path).status_code == 401, path


def test_login_sets_a_session_and_logout_clears_it(client):
    _login(client)
    assert client.get("/ui/api/me").json()["user"]["username"] == USERNAME
    client.post("/ui/api/logout")
    assert client.get("/ui/api/me").status_code == 401


def test_login_rejects_a_bad_password(client):
    response = client.post("/ui/api/login", json={"username": USERNAME, "password": "mal"})
    assert response.status_code == 401


def test_user_creates_own_key_and_sees_it_once(client):
    _login(client)
    created = client.post("/ui/api/keys", json={"label": "script", "expires_in_days": 30})
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    assert key.startswith("dulse_")
    listed = client.get("/ui/api/keys").json()
    assert any(k["visible_prefix"] == key[:12] for k in listed)
    assert all(key not in str(k) for k in listed)


def test_sixth_key_is_rejected(client):
    _login(client)
    active = len([k for k in client.get("/ui/api/keys").json() if k["active"]])
    for _ in range(auth_db.MAX_ACTIVE_KEYS - active):
        assert client.post("/ui/api/keys", json={"expires_in_days": 30}).status_code == 201
    sixth = client.post("/ui/api/keys", json={"expires_in_days": 30})
    assert sixth.status_code == 400
    assert "máximo 5" in sixth.json()["detail"]


def test_expiry_over_180_days_is_rejected(client):
    _login(client)
    response = client.post("/ui/api/keys", json={"expires_in_days": 181})
    assert response.status_code == 400
    assert "180" in response.json()["detail"]


def test_rotate_replaces_the_key(client):
    _login(client)
    created = client.post("/ui/api/keys", json={"expires_in_days": 30}).json()
    rotated = client.post(f"/ui/api/keys/{created['id']}/rotate", json={"expires_in_days": 60})
    assert rotated.status_code == 201, rotated.text
    assert rotated.json()["key"] != created["key"]


def test_revoked_key_stops_working_against_the_api(client):
    _login(client)
    created = client.post("/ui/api/keys", json={"expires_in_days": 30}).json()
    key = created["key"]
    headers = {"X-API-Key": key}
    assert client.get("/api/v1/tables", headers=headers).status_code == 200
    assert client.post(f"/ui/api/keys/{created['id']}/revoke").status_code == 200
    auth._API_KEY_CACHE.clear()  # el cache de 5s no aplica en el test
    assert client.get("/api/v1/tables", headers=headers).status_code == 401


def test_only_superadmin_manages_users(client):
    _login(client)
    assert client.get("/ui/api/users").status_code == 403
    denied = client.post("/ui/api/users", json={"username": "x", "password": "password-largo"})
    assert denied.status_code == 403


def test_superadmin_creates_a_user_that_can_log_in_and_use_a_key(client):
    _login(client, SUPERADMIN_USERNAME, SUPERADMIN_PASSWORD)
    created = client.post(
        "/ui/api/users", json={"username": "beto", "password": "beto-password-1"}
    )
    assert created.status_code == 201, created.text
    usernames = [u["username"] for u in client.get("/ui/api/users").json()]
    assert usernames == [SUPERADMIN_USERNAME, USERNAME, "beto"]
    client.post("/ui/api/logout")
    _login(client, "beto", "beto-password-1")
    key = client.post("/ui/api/keys", json={"expires_in_days": 1}).json()["key"]
    assert client.get("/api/v1/tables", headers={"X-API-Key": key}).status_code == 200


def test_disabled_user_cannot_log_in(client):
    _login(client, SUPERADMIN_USERNAME, SUPERADMIN_PASSWORD)
    users = client.get("/ui/api/users").json()
    target = next(u for u in users if u["username"] == USERNAME)
    assert client.post(f"/ui/api/users/{target['id']}/disable").status_code == 200
    client.post("/ui/api/logout")
    response = client.post("/ui/api/login", json={"username": USERNAME, "password": PASSWORD})
    assert response.status_code == 401
