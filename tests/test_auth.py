"""Tests de autenticación: API keys (desde la base) + JWT + panel."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from dulse.infrastructure import security as auth
from dulse.adapters.inbound.http.api import app

from tests.support import PASSWORD, TEST_API_KEY, USERNAME

VALID_KEY = TEST_API_KEY


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_docs_are_not_public(client):
    """El esquema de la API no se publica: pide sesión del panel."""
    response = client.get("/openapi.json", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/panel/entrar")


def test_panel_login_page_is_public(client):
    """La página de entrada del panel carga sin credenciales."""
    response = client.get("/auth/panel/entrar")
    assert response.status_code == 200
    assert "Entrar al panel" in response.text


def test_protected_route_without_credentials_is_401(client):
    response = client.get("/api/v1/tables")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_api_key_is_401(client):
    response = client.get("/api/v1/tables", headers={"X-API-Key": "nope"})
    assert response.status_code == 401


def test_valid_api_key_reaches_the_endpoint(client):
    response = client.get("/api/v1/tables", headers={"X-API-Key": VALID_KEY})
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_mcp_mount_requires_credentials(client):
    response = client.get("/mcp/sse")
    assert response.status_code == 401


# --- JWT (usuarios de la base) ---------------------------------------------


def test_token_endpoint_returns_usable_jwt(client):
    response = client.post(
        "/auth/token", json={"username": USERNAME, "password": PASSWORD}
    )
    assert response.status_code == 200
    token = response.json()["access_token"]
    assert token.count(".") == 2
    protected = client.get("/api/v1/tables", headers={"Authorization": f"Bearer {token}"})
    assert protected.status_code == 200


def test_token_endpoint_rejects_bad_password(client):
    response = client.post("/auth/token", json={"username": USERNAME, "password": "mal"})
    assert response.status_code == 401


def test_token_endpoint_rejects_unknown_user(client):
    response = client.post("/auth/token", json={"username": "nadie", "password": PASSWORD})
    assert response.status_code == 401


def test_expired_token_is_401(client):
    token, _ = auth.create_access_token("ana", now=time.time() - 7200, ttl_minutes=1)
    response = client.get("/api/v1/tables", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_invalid_signature_is_401(client):
    token, _ = auth.create_access_token("ana")
    broken = token[:-4] + "aaaa"
    response = client.get("/api/v1/tables", headers={"Authorization": f"Bearer {broken}"})
    assert response.status_code == 401


# --- hashing y comparaciones -----------------------------------------------


def test_hash_password_roundtrip():
    stored = auth.hash_password("s3cr3t")
    assert stored.startswith("pbkdf2_sha256$")
    assert auth.verify_password(stored, "s3cr3t") is True
    assert auth.verify_password(stored, "s3cr3t!") is False


def test_plaintext_password_is_supported():
    assert auth.verify_password("plain-pass", "plain-pass") is True
    assert auth.verify_password("plain-pass", "other") is False


def test_missing_jwt_secret_fails_closed(monkeypatch):
    """Sin secreto de firma no hay JWT: el arranque tiene que cortar."""
    monkeypatch.setattr(auth, "jwt_secret", lambda: "")
    with pytest.raises(RuntimeError, match="AUTH_JWT_SECRET"):
        auth.assert_configured()


def test_non_ascii_api_key_is_rejected_not_crashing():
    """Una API key no-ASCII debe ser credencial inválida, nunca un TypeError."""
    with pytest.raises(auth.AuthError):
        auth.subject_from_headers({"x-api-key": "clave-con-ñ"})


def test_non_ascii_password_is_401_not_500(client):
    response = client.post(
        "/auth/token", json={"username": USERNAME, "password": "contraseña-ñandú"}
    )
    assert response.status_code == 401


def test_unicode_plaintext_password_roundtrip():
    assert auth.verify_password("contraseña-ñandú", "contraseña-ñandú") is True
    assert auth.verify_password("contraseña-ñandú", "contrasena-nandu") is False


def test_unicode_pbkdf2_password_roundtrip():
    stored = auth.hash_password("contraseña-ñandú", iterations=1000)
    assert auth.verify_password(stored, "contraseña-ñandú") is True
    assert auth.verify_password(stored, "otra") is False


def test_zero_iterations_hash_is_rejected_not_crashing():
    assert auth.verify_password("pbkdf2_sha256$0$salt$deadbeef", "x") is False
    assert auth.verify_password("pbkdf2_sha256$-5$salt$deadbeef", "x") is False


def test_excessive_iterations_hash_is_rejected_without_hanging():
    assert auth.verify_password("pbkdf2_sha256$1000000000$salt$deadbeef", "x") is False


def test_disabling_a_user_invalidates_its_issued_jwt(client):
    """Un JWT ya emitido no puede sobrevivir al deshabilitado del usuario.

    El camino Bearer sólo verificaba firma y subject: sin este chequeo, un
    usuario deshabilitado seguía entrando a los datos hasta que venciera el token.
    """
    from dulse.adapters.outbound import postgres_auth as auth_db

    with auth_db.connection() as conn:
        user = auth_db.verify_login(conn, USERNAME, PASSWORD)
    token, _ = auth.create_access_token(USERNAME)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/tables", headers=headers).status_code == 200

    # Sin cache: el token se rechaza en el request siguiente, sin trucos.

    with auth_db.connection() as conn:
        auth_db.disable_user(conn, user["id"])
    assert client.get("/api/v1/tables", headers=headers).status_code == 401

    # Restituye el estado para el resto de la suite.
    with auth_db.connection() as conn:
        conn.execute("UPDATE users SET disabled_at = NULL WHERE id = %s", (user["id"],))
        conn.commit()
