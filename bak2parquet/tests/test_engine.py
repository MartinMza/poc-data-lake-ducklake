"""Tests sin motor: nombre generado, max memory y argv del contenedor (T3/S1)."""

import pytest

from bak2parquet.config import CONTAINER_CPUS, CONTAINER_MEM_LIMIT, MSSQL_MEMORY_LIMIT_MB
from bak2parquet.engine import (
    GENERATED_DB_RE,
    EngineError,
    UrlEngine,
    apply_max_server_memory,
    docker_run_argv,
    generate_database_name,
    max_server_memory_sql,
    wait_ready,
)


def test_nombre_generado_no_deriva_del_bak():
    nombre = generate_database_name(pid=123, now_ts=456)
    assert nombre == "bak2pq_123_456"
    assert GENERATED_DB_RE.fullmatch(nombre)
    assert "FAF12_Kavos" not in generate_database_name(pid=1, now_ts=2)
    assert "test-source" not in generate_database_name(pid=1, now_ts=2)


def test_max_server_memory_acotado():
    sql = max_server_memory_sql()
    # Regresión (encontrada por la integración real): sin habilitar 'show
    # advanced options' primero, sp_configure falla con 15123 en un motor
    # recién arrancado, que es justo el caso efímero.
    assert "show advanced options" in sql
    assert sql.index("show advanced options") < sql.index("max server memory (MB)")
    assert "1024" in sql and sql.count("RECONFIGURE") == 2
    with pytest.raises(ValueError):
        max_server_memory_sql(1800)  # S1: nunca más de 1200.


def test_docker_argv_con_limites():
    argv = docker_run_argv(image="img", container_name="c", password="p",
                            host_port=11433, bak_dir="/tmp/baks")
    assert "--memory" in argv and CONTAINER_MEM_LIMIT in argv  # 1600m.
    assert "--cpus" in argv and CONTAINER_CPUS in argv  # 1.5.
    assert f"MSSQL_MEMORY_LIMIT_MB={MSSQL_MEMORY_LIMIT_MB}" in argv  # 1300.
    assert "/tmp/baks:/var/opt/mssql/backup:ro" in argv
    assert "127.0.0.1:11433:1433" in argv


def test_wait_ready_reintenta_y_aborta():
    intentos = {"n": 0}

    def conectar():
        intentos["n"] += 1
        raise ConnectionError("todavía no")

    with pytest.raises(EngineError):
        wait_ready(conectar, timeout_sec=1, interval_sec=0.2)
    assert intentos["n"] > 1


class _CursorOk:
    def execute(self, _sql):
        return None


class _ConnOk:
    def cursor(self):
        return _CursorOk()

    def close(self):
        return None


def test_wait_ready_ok_y_max_memory_ok():
    wait_ready(lambda: _ConnOk(), timeout_sec=5)
    apply_max_server_memory(_ConnOk())  # No lanza.


def test_apply_max_memory_propaga_motivo():
    class _Mala:
        def cursor(self):
            raise RuntimeError("Login failed for user 'sa'")

    with pytest.raises(EngineError, match="Login failed"):
        apply_max_server_memory(_Mala())


def test_url_stop_nunca_detiene():
    engine = UrlEngine.__new__(UrlEngine)  # Sin config: stop no toca nada.
    assert engine.stop() is None
