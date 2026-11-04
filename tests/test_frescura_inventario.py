"""Frescura del inventario: de dónde sale y que no vuelva a ser `None`.

Contexto del defecto: `sqlserver_tables()` documentaba un campo `last_load_id`
para saber si una tabla estaba fresca. Lo leía de `_dlt_load_id`, una columna
que sólo escribía dlt, retirado hace varios commits. El resultado era `None` en
las 788 tablas: ningún error, ningún test, un dato que se perdió en silencio y
que cualquier consumidor podía leer como "no sé" o como "está viejo".

Estos tests cubren la derivación nueva, que usa los snapshots de DuckLake
—el registro real de escrituras— contra un catálogo DuckLake temporal de
verdad, no un mock del motor.
"""

from __future__ import annotations

import duckdb
import pytest

from kavos_lake.adapters.outbound.duckdb_engine import _ultimas_escrituras


@pytest.fixture()
def lago(tmp_path):
    """Catálogo DuckLake real en un directorio temporal."""
    con = duckdb.connect()
    con.execute("LOAD ducklake")
    con.execute(
        f"ATTACH 'ducklake:{tmp_path}/catalogo.ducklake' AS lake "
        f"(DATA_PATH '{tmp_path}/data')"
    )
    con.execute("CREATE SCHEMA lake.bronze")
    yield con
    con.close()


def test_una_tabla_escrita_una_vez_reporta_su_snapshot_y_su_hora(lago):
    lago.execute("CREATE TABLE lake.bronze.mssql_moneda AS SELECT 1 AS x")

    ultimas = _ultimas_escrituras(lago.cursor())

    assert "bronze.mssql_moneda" in ultimas
    snapshot_id, momento = ultimas["bronze.mssql_moneda"]
    assert isinstance(snapshot_id, int)
    # La hora es la mitad del dato: sin fecha, la frescura no se puede leer.
    assert momento is not None


def test_una_tabla_recreada_reporta_la_escritura_mas_reciente(lago):
    """La ingesta reemplaza la tabla entera: gana el último CREATE, no el primero."""
    lago.execute("CREATE TABLE lake.bronze.mssql_pedido AS SELECT 1 AS x")
    primero, _ = _ultimas_escrituras(lago.cursor())["bronze.mssql_pedido"]

    lago.execute("DROP TABLE lake.bronze.mssql_pedido")
    lago.execute("CREATE TABLE lake.bronze.mssql_pedido AS SELECT 2 AS x")

    segundo, _ = _ultimas_escrituras(lago.cursor())["bronze.mssql_pedido"]

    assert segundo > primero


def test_varias_tablas_se_reportan_por_separado(lago):
    lago.execute("CREATE TABLE lake.bronze.mssql_a AS SELECT 1 AS x")
    lago.execute("CREATE TABLE lake.bronze.mssql_b AS SELECT 2 AS x")

    ultimas = _ultimas_escrituras(lago.cursor())

    assert {"bronze.mssql_a", "bronze.mssql_b"} <= set(ultimas)


def test_sin_escrituras_registradas_no_inventa_entradas(lago):
    """Sin snapshots con tablas creadas, el mapa queda vacío y nadie recibe datos falsos."""
    assert _ultimas_escrituras(lago.cursor()) == {}
