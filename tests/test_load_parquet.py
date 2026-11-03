"""Cargador Parquet → Bronze: contrato de nombres, reemplazo e idempotencia.

La unidad que reemplazó el camino dlt/bcp no podía escribir tests (su superficie
no incluía `tests/`), así que el cargador quedó sin cobertura. Estos tests fijan
lo que sí se puede verificar sin lago vivo: normalización de nombres, reemplazo
total por tabla (no append), tabla vacía con esquema y colisión detectada.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bronze" / "load_parquet.py"
_spec = importlib.util.spec_from_file_location("load_parquet", MODULE_PATH)
load_parquet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(load_parquet)


def _parquet(path: Path, select: str) -> Path:
    """Genera un Parquet con DuckDB (sin pyarrow: el venv raíz no lo trae)."""
    con = duckdb.connect()
    con.execute(f"COPY ({select}) TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.close()
    return path


@pytest.fixture()
def con():
    connection = duckdb.connect()
    connection.execute("CREATE SCHEMA bronze")
    yield connection
    connection.close()


def test_snake_normaliza_el_contrato_del_lago():
    assert load_parquet.snake("TransaccionID") == "transaccion_id"
    assert load_parquet.snake("ImporteTotal") == "importe_total"


def test_dest_table_conserva_el_contrato_mssql():
    # Es el nombre que la API y el MCP ya exponen: no puede cambiar.
    assert load_parquet.dest_table("dbo", "BSTransaccion") == "mssql_bs_transaccion"
    # Los esquemas no-`dbo` anteponen su nombre pero conservan el prefijo del lago.
    assert load_parquet.dest_table("ventas", "Pedido") == "mssql_ventas_pedido"


def test_load_one_reemplaza_en_vez_de_acumular(con, tmp_path):
    primera = _parquet(tmp_path / "a.parquet", "SELECT 1 AS TransaccionID, 10.5 AS ImporteTotal")
    assert load_parquet.load_one(con, "mssql_prueba", str(primera)) == 1

    segunda = _parquet(
        tmp_path / "b.parquet",
        "SELECT * FROM (VALUES (1, 1.0), (2, 2.0), (3, 3.0)) AS t(TransaccionID, ImporteTotal)",
    )
    assert load_parquet.load_one(con, "mssql_prueba", str(segunda)) == 3

    filas = con.execute("SELECT COUNT(*) FROM bronze.mssql_prueba").fetchone()[0]
    assert filas == 3, "un segundo load debe reemplazar, no sumar"
    columnas = [row[0] for row in con.execute("DESCRIBE bronze.mssql_prueba").fetchall()]
    assert columnas == ["transaccion_id", "importe_total"]


def test_tabla_vacia_conserva_el_esquema(con, tmp_path):
    vacia = _parquet(
        tmp_path / "vacia.parquet",
        "SELECT CAST(1 AS BIGINT) AS Id, CAST('x' AS VARCHAR) AS Nombre WHERE 1 = 0",
    )
    assert load_parquet.load_one(con, "mssql_vacia", str(vacia)) == 0
    columnas = [row[0] for row in con.execute("DESCRIBE bronze.mssql_vacia").fetchall()]
    assert columnas == ["id", "nombre"]


def test_columnas_que_colisionan_abortan(con, tmp_path):
    # "Foo-Bar" y "Foo_Bar" normalizan al mismo nombre: pisar en silencio sería
    # perder una columna real.
    choque = _parquet(
        tmp_path / "choque.parquet",
        'SELECT 1 AS "Foo-Bar", 2 AS "Foo_Bar"',
    )
    with pytest.raises(SystemExit, match="colisiona"):
        load_parquet.load_one(con, "mssql_choque", str(choque))
