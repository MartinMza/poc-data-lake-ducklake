"""Endurecimiento de configuración y errores (hallazgos BAJO #5, #7, #8).

- #5: no desplegar credenciales de ejemplo contra un host público.
- #7: AUTH_JWT_SECRET con entropía mínima.
- #8: los 500 no filtran el detalle de la excepción al cliente.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import auth, config, test_support
from api.main import app


# --- #5: guard de credenciales por defecto -----------------------------------


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "::1", "postgres-catalog", "garage", "10.0.0.5",
     "192.168.1.10", "172.16.4.2", "host.docker.internal", "svc.local", ""],
)
def test_hosts_internos_no_se_consideran_expuestos(host):
    assert config.is_internal_host(host) is True


@pytest.mark.parametrize("host", ["api.example.com", "8.8.8.8", "1.1.1.1"])
def test_hosts_publicos_se_consideran_expuestos(host):
    assert config.is_internal_host(host) is False


def test_default_contra_host_interno_no_falla():
    config.assert_safe_credentials(host="postgres-catalog", secret="ducklake", label="x")


def test_default_contra_host_publico_falla():
    with pytest.raises(RuntimeError, match="credencial por defecto"):
        config.assert_safe_credentials(host="db.example.com", secret="ducklake", label="x")


def test_secreto_fuerte_contra_host_publico_pasa():
    config.assert_safe_credentials(
        host="db.example.com", secret="Zx9-long-random-passphrase", label="x"
    )


# --- #7: entropía del secreto JWT --------------------------------------------


def test_secreto_jwt_corto_falla(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", "corto")
    with pytest.raises(RuntimeError, match="demasiado corto"):
        auth.assert_configured()


def test_secreto_jwt_ausente_falla(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", "")
    with pytest.raises(RuntimeError, match="AUTH_JWT_SECRET"):
        auth.assert_configured()


def test_secreto_jwt_largo_pasa(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", "x" * auth.MIN_JWT_SECRET_LENGTH)
    auth.assert_configured()


# --- #8: 500 sin fuga de detalle ---------------------------------------------


def test_error_500_no_filtra_el_detalle(monkeypatch):
    secret_detail = "postgres://user:pass@host/db ruta=/app/.env"

    def boom(**kwargs):
        raise RuntimeError(secret_detail)

    monkeypatch.setattr("api.main.engine.list_tables", boom)
    with TestClient(app, headers=test_support.auth_headers()) as client:
        response = client.get("/api/v1/tables")
    assert response.status_code == 500
    body = response.text
    assert "Error interno" in body
    assert "pass" not in body and ".env" not in body and "postgres://" not in body
