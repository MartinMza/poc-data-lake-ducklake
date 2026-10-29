"""Los shims de `api/` son el MISMO objeto que el módulo real.

Si un shim dejara de reemplazarse en sys.modules, el estado mutable (singletons
como el throttle y el servidor MCP) se duplicaría y los entrypoints verían otro
objeto. Este test lo fija.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI

PAIRS = [
    ("api.config", "dulse.infrastructure.config"),
    ("api.auth", "dulse.infrastructure.security"),
    ("api.auth_db", "dulse.adapters.outbound.postgres_auth"),
    ("api.engine", "dulse.adapters.outbound.duckdb_engine"),
    ("api.throttle", "dulse.adapters.outbound.memory_throttle"),
    ("api.auth_panel", "dulse.adapters.inbound.http.panel"),
    ("api.mcp_server", "dulse.adapters.inbound.mcp.server"),
    ("api.main", "dulse.adapters.inbound.http.api"),
]


@pytest.mark.parametrize("shim,target", PAIRS)
def test_shim_es_el_modulo_real(shim, target):
    assert importlib.import_module(shim) is importlib.import_module(target)


def test_el_singleton_del_throttle_es_compartido():
    import api.throttle
    from dulse.adapters.outbound import memory_throttle

    assert api.throttle.login_throttle is memory_throttle.login_throttle


def test_api_main_expone_la_app_fastapi():
    import api.main

    assert isinstance(api.main.app, FastAPI)
