"""pydantic-settings: validación fail-closed, secretos protegidos y valores derivados.

Antes de la migración, un valor inválido caía silenciosamente a un default
(p. ej. `AUTH_LOGIN_MAX_ATTEMPTS=0` desactivaba el rate-limit sin avisar) y las
credenciales eran `str` planos que un log podía filtrar.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kavos_lake.infrastructure import config


# --- validación ---------------------------------------------------------------


def test_settings_valida_rango_de_enteros(monkeypatch):
    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "0")
    with pytest.raises(ValidationError):
        config.get_settings()


def test_settings_rechaza_booleano_invalido(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "tal vez")
    with pytest.raises(ValidationError):
        config.get_settings()


def test_settings_rechaza_puerto_fuera_de_rango(monkeypatch):
    monkeypatch.setenv("CATALOG_PORT", "70000")
    with pytest.raises(ValidationError):
        config.get_settings()


def test_defaults_del_modelo_cuando_no_hay_entorno(monkeypatch):
    monkeypatch.delenv("AUTH_LOGIN_MAX_ATTEMPTS", raising=False)
    assert config.get_settings().auth_login_max_attempts == 5


# --- secretos -----------------------------------------------------------------


def test_los_secretos_no_aparecen_en_el_repr(monkeypatch):
    monkeypatch.setenv("CATALOG_PASSWORD", "s3cr3t-catalogo")
    monkeypatch.setenv("AUTH_JWT_SECRET", "s3cr3t-jwt")
    settings = config.get_settings()
    volcado = repr(settings) + str(settings)
    assert "s3cr3t-catalogo" not in volcado
    assert "s3cr3t-jwt" not in volcado
    assert settings.catalog_password.get_secret_value() == "s3cr3t-catalogo"


# --- valores derivados (nada combinado en una variable) ------------------------


def test_la_ruta_del_lago_se_deriva_de_bucket_y_prefijo(monkeypatch):
    monkeypatch.setenv("LAKEHOUSE_BUCKET", "bucket-x")
    monkeypatch.setenv("DUCKLAKE_DATA_PREFIX", "/anidado/data/")
    assert config.get_settings().ducklake_data_path == "s3://bucket-x/anidado/data/"


def test_la_ruta_del_lago_sin_prefijo_termina_en_barra(monkeypatch):
    monkeypatch.setenv("LAKEHOUSE_BUCKET", "solo-bucket")
    monkeypatch.setenv("DUCKLAKE_DATA_PREFIX", "")
    assert config.get_settings().ducklake_data_path == "s3://solo-bucket/"


def test_el_endpoint_de_garage_se_deriva_de_host_y_puerto(monkeypatch):
    monkeypatch.setenv("GARAGE_HOST", "garage.svc")
    monkeypatch.setenv("GARAGE_S3_PORT", "3901")
    assert config.get_settings().garage_s3_endpoint == "garage.svc:3901"


def test_ssl_se_deriva_del_esquema(monkeypatch):
    monkeypatch.setenv("GARAGE_SCHEME", "https")
    assert config.get_settings().garage_use_ssl is True
    monkeypatch.setenv("GARAGE_SCHEME", "http")
    assert config.get_settings().garage_use_ssl is False


def test_la_base_de_credenciales_no_hereda_del_catalogo(monkeypatch):
    """Tiene variables propias: apuntar el catálogo a otro host no la mueve."""
    monkeypatch.setenv("CATALOG_HOST", "catalogo.example")
    monkeypatch.delenv("AUTH_DB_HOST", raising=False)
    assert config.get_settings().auth_db_host == "localhost"


def test_el_modelo_no_expone_variables_combinadas():
    campos = set(config.Settings.model_fields)
    assert "garage_s3_endpoint" not in campos
    assert "ducklake_data_path" not in campos
    assert "auth_db_dsn" not in campos


def test_el_dsn_de_credenciales_se_deriva(monkeypatch):
    monkeypatch.setenv("AUTH_DB_HOST", "auth.example")
    monkeypatch.setenv("AUTH_DB_PORT", "6543")
    monkeypatch.setenv("AUTH_DB_NAME", "mi_base")
    dsn = config.get_settings().auth_db_dsn
    assert "host=auth.example" in dsn
    assert "port=6543" in dsn
    assert "dbname=mi_base" in dsn
