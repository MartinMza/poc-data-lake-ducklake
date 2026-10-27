"""Tests de autenticación: API keys (servicio) + JWT (usuarios)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from api import auth
from api.main import app

# Las credenciales de prueba (`AUTH_API_KEYS`, `AUTH_JWT_SECRET`, `AUTH_USERS`) se
# definen en `api/conftest.py`: acá sólo se referencian.
VALID_KEY = "test-key-00000000000000001"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_docs_are_public(client):
    assert client.get("/openapi.json").status_code == 200


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


def test_token_endpoint_returns_usable_jwt(client):
    response = client.post(
        "/auth/token", json={"username": "admin", "password": "test-password"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "bearer"
    assert payload["expires_in"] > 0

    protected = client.get(
        "/api/v1/tables",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
    )
    assert protected.status_code == 200


def test_token_endpoint_rejects_bad_password(client):
    response = client.post(
        "/auth/token", json={"username": "admin", "password": "wrong"}
    )
    assert response.status_code == 401


def test_token_endpoint_rejects_unknown_user(client):
    response = client.post(
        "/auth/token", json={"username": "ghost", "password": "test-password"}
    )
    assert response.status_code == 401


def test_expired_token_is_401(client):
    token, _ = auth.create_access_token("admin", now=time.time() - 7200, ttl_minutes=60)
    response = client.get("/api/v1/tables", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_invalid_signature_is_401(client):
    token, _ = auth.create_access_token("admin")
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    response = client.get(
        "/api/v1/tables", headers={"Authorization": f"Bearer {tampered}"}
    )
    assert response.status_code == 401


def test_hash_password_roundtrip():
    stored = auth.hash_password("s3cr3t", iterations=2000)
    assert stored.startswith("pbkdf2_sha256$")
    assert auth.verify_password(stored, "s3cr3t") is True
    assert auth.verify_password(stored, "s3cr3t!") is False


def test_plaintext_password_is_supported():
    assert auth.verify_password("plain-pass", "plain-pass") is True
    assert auth.verify_password("plain-pass", "other") is False


def test_missing_configuration_fails_closed(monkeypatch):
    monkeypatch.setattr(auth, "api_keys", lambda: ())
    monkeypatch.setattr(auth, "users", lambda: {})
    with pytest.raises(RuntimeError, match="AUTH_API_KEYS"):
        auth.assert_configured()


def test_non_ascii_api_key_is_rejected_not_crashing():
    """Una API key no-ASCII debe ser credencial inválida, nunca un TypeError.

    No se prueba por header HTTP porque el transporte no admite valores
    no-ASCII: se ejerce la resolución de credenciales directamente.
    """
    with pytest.raises(auth.AuthError):
        auth.subject_from_headers({"x-api-key": "clave-con-ñ"})


def test_non_ascii_password_is_401_not_500(client):
    response = client.post(
        "/auth/token", json={"username": "admin", "password": "contraseña-ñandú"}
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
    """Iteraciones 0 hacen que pbkdf2_hmac lance ValueError: debe ser credencial
    inválida (401), nunca una excepción sin capturar (500)."""
    assert auth.verify_password("pbkdf2_sha256$0$salt$deadbeef", "x") is False
    assert auth.verify_password("pbkdf2_sha256$-5$salt$deadbeef", "x") is False


def test_excessive_iterations_hash_is_rejected_without_hanging():
    """Un AUTH_USERS con iteraciones enormes colgaría el worker en cada login:
    se rechaza antes de derivar la clave."""
    assert auth.verify_password("pbkdf2_sha256$1000000000$salt$deadbeef", "x") is False


def test_zero_iterations_login_is_401_not_500(client, monkeypatch):
    monkeypatch.setattr(
        auth, "users", lambda: {"admin": "pbkdf2_sha256$0$salt$deadbeef"}
    )
    response = client.post(
        "/auth/token", json={"username": "admin", "password": "x"}
    )
    assert response.status_code == 401


def test_short_api_key_fails_closed(monkeypatch):
    """Una API key corta (placeholder tipo 'cambiame') no habilita el servicio."""
    monkeypatch.setattr(auth, "api_keys", lambda: ("cambiame",))
    monkeypatch.setattr(auth, "users", lambda: {})
    with pytest.raises(RuntimeError, match="demasiado corta"):
        auth.assert_configured()


def test_malformed_users_entry_fails_closed(monkeypatch):
    """Una entrada de AUTH_USERS sin separador se descartaba en silencio:
    ahora el arranque falla nombrando el problema."""
    monkeypatch.setenv("AUTH_USERS", "admin:un-password,sin-separador")
    monkeypatch.setattr(auth, "api_keys", lambda: ())
    with pytest.raises(RuntimeError, match="entrada inválida"):
        auth.assert_configured()
