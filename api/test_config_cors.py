"""CORS configurable por entorno y el parser `env_list`.

El default inseguro era `allow_origins=['*']` con `allow_credentials=True`.
Ahora los orígenes y las credenciales se leen del entorno, con una guarda que
desactiva las credenciales si alguien deja `*` (combinación que el navegador
rechaza y que, de aceptarse, abriría el panel a cualquier origen).
"""

from __future__ import annotations

import pytest

from api import config, main


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ALLOW_CREDENTIALS", raising=False)
    yield


# --- env_list ----------------------------------------------------------------


def test_env_list_parsea_lista_separada_por_comas(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://a.example, https://b.example ,")
    assert config.env_list("CORS_ALLOW_ORIGINS", ("x",)) == (
        "https://a.example",
        "https://b.example",
    )


def test_env_list_usa_default_si_no_esta(monkeypatch):
    assert config.env_list("CORS_ALLOW_ORIGINS", ("http://localhost:8000",)) == (
        "http://localhost:8000",
    )


# --- cors_config -------------------------------------------------------------


def test_cors_default_no_es_wildcard_ni_credenciales():
    origins, credentials = main.cors_config()
    assert "*" not in origins
    assert credentials is False
    assert all(o.startswith("http") for o in origins)


def test_cors_toma_origenes_del_entorno(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://dash.example,https://app.example")
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "true")
    origins, credentials = main.cors_config()
    assert origins == ["https://dash.example", "https://app.example"]
    assert credentials is True


def test_cors_wildcard_con_credenciales_desactiva_credenciales(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "true")
    origins, credentials = main.cors_config()
    assert origins == ["*"]
    assert credentials is False  # la guarda gana
