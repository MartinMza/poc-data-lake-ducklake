"""pydantic-settings: validación fail-closed de las perillas sensibles.

Antes de S1 un valor inválido caía silenciosamente a un default (p. ej.
`AUTH_LOGIN_MAX_ATTEMPTS=0` desactivaba el rate-limit sin avisar). Ahora el
tipo y el rango se validan al leer la configuración.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kavos_lake.infrastructure import config


def test_settings_valida_rango_de_enteros(monkeypatch):
    monkeypatch.setenv("AUTH_LOGIN_MAX_ATTEMPTS", "0")
    with pytest.raises(ValidationError):
        config.get_settings()


def test_settings_rechaza_booleano_invalido(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "tal vez")
    with pytest.raises(ValidationError):
        config.get_settings()


def test_env_int_valida_el_valor_cuando_esta_presente(monkeypatch):
    monkeypatch.setenv("AUTH_LOGIN_LOCKOUT_SECONDS", "abc")
    with pytest.raises(ValidationError):
        config.env_int("AUTH_LOGIN_LOCKOUT_SECONDS", 300)


def test_settings_parsea_origenes_csv_con_espacios(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOW_ORIGINS", " https://a.example , https://b.example "
    )
    assert config.get_settings().cors_allow_origins == [
        "https://a.example",
        "https://b.example",
    ]


def test_env_int_usa_default_del_llamador_si_no_esta(monkeypatch):
    monkeypatch.delenv("AUTH_LOGIN_MAX_ATTEMPTS", raising=False)
    assert config.env_int("AUTH_LOGIN_MAX_ATTEMPTS", 7) == 7
