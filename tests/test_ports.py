"""Los adaptadores de salida cumplen los puertos declarados.

Si un adaptador deja de exponer un método del contrato, este test lo detecta
antes de que lo haga la capa de entrada.
"""

from __future__ import annotations

from kavos_lake.adapters.outbound import duckdb_engine, memory_throttle, postgres_auth
from kavos_lake.ports.engine import QueryEnginePort
from kavos_lake.ports.identity import (
    ApiKeyRepositoryPort,
    SessionRepositoryPort,
    ThrottlePort,
    UserRepositoryPort,
)


def test_duckdb_engine_cumple_el_puerto_de_motor():
    assert isinstance(duckdb_engine, QueryEnginePort)


def test_postgres_auth_cumple_los_puertos_de_identidad():
    assert isinstance(postgres_auth, UserRepositoryPort)
    assert isinstance(postgres_auth, SessionRepositoryPort)
    assert isinstance(postgres_auth, ApiKeyRepositoryPort)


def test_memory_throttle_cumple_el_puerto_de_throttle():
    assert isinstance(memory_throttle.login_throttle, ThrottlePort)
