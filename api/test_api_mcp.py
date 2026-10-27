"""Tests de integración de la API REST y el MCP contra la data real de SQL Server.

El lake debe contener únicamente las 7 tablas `mssql_*` ingeridas desde SQL
Server (más metadatos internos de dlt). La autenticación se cubre en
`api/test_auth.py`.
"""

import json

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.mcp_server import (
    describe_table as mcp_describe_table,
    get_catalog_resource,
    get_sqlserver_catalog_resource,
    list_snapshots as mcp_list_snapshots,
    list_tables as mcp_list_tables,
    preview_rows as mcp_preview_rows,
    run_query as mcp_run_query,
    sqlserver_tables as mcp_sqlserver_tables,
)

AUTH_HEADERS = {"X-API-Key": "test-key-00000000000000001"}

REAL_TABLES = {
    "mssql_bs_moneda",
    "mssql_bs_operacion",
    "mssql_bs_organizacion",
    "mssql_bs_producto",
    "mssql_bs_transaccion",
    "mssql_d_mart_ventas",
    "mssql_d_mart_ventas_producto_cliente",
}

SYNTHETIC_TABLES = {
    "customers",
    "units",
    "leases",
    "payments",
    "mssql_productos",
    "mssql_ventas",
    "mssql_movimientos",
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app, headers=AUTH_HEADERS) as test_client:
        yield test_client


# === Catálogo: solo data de SQL Server ===
def test_list_tables_contains_only_sqlserver_tables(client):
    response = client.get("/api/v1/tables")
    assert response.status_code == 200
    tables = response.json()

    names = {t["table"] for t in tables}
    assert REAL_TABLES <= names
    assert not (names & SYNTHETIC_TABLES)
    assert all(name.startswith(("mssql_", "_dlt_")) for name in names)
    assert {t["schema"] for t in tables} == {"bronze"}


def test_describe_real_table(client):
    response = client.get("/api/v1/tables/bronze/mssql_bs_transaccion")
    assert response.status_code == 200
    data = response.json()
    assert data["row_count"] > 300_000
    col_names = {c["name"] for c in data["columns"]}
    # El fast path (bcp -> staging -> MERGE) normaliza los nombres a snake_case.
    assert {"transaccion_id", "importe_total"} <= col_names
    assert 0 < len(data["sample"]) <= 5


def test_describe_table_not_found(client):
    assert client.get("/api/v1/tables/bronze/tabla_inexistente").status_code == 404


def test_describe_table_invalid_identifier(client):
    assert client.get("/api/v1/tables/bronze/tabla;drop").status_code == 400


# === Inventario SQL Server ===
def test_sqlserver_tables_inventory(client):
    response = client.get("/api/v1/sqlserver/tables")
    assert response.status_code == 200
    inventory = response.json()

    assert {row["table"] for row in inventory} == REAL_TABLES
    for row in inventory:
        assert row["schema"] == "bronze"
        assert row["row_count"] > 0
        assert row["column_count"] == len(row["columns"])
        assert row["last_load_id"], f"{row['table']} sin _dlt_load_id"


def test_preview_rows_paginates_real_data(client):
    inventory = {row["table"]: row for row in client.get("/api/v1/sqlserver/tables").json()}
    total = inventory["mssql_bs_moneda"]["row_count"]

    first_page = client.get(
        "/api/v1/sqlserver/tables/mssql_bs_moneda/rows", params={"limit": 5}
    )
    assert first_page.status_code == 200
    page = first_page.json()
    assert page["total_rows"] == total
    assert page["row_count"] == min(5, total)
    assert "moneda_id" in page["columns"]

    tail = client.get(
        "/api/v1/sqlserver/tables/mssql_bs_moneda/rows",
        params={"limit": 5, "offset": max(total - 2, 0)},
    ).json()
    assert tail["row_count"] == min(2, total)


def test_preview_rows_unknown_table_is_404(client):
    response = client.get("/api/v1/sqlserver/tables/no_existe/rows")
    assert response.status_code == 404


def test_preview_rows_invalid_table_is_400(client):
    response = client.get("/api/v1/sqlserver/tables/bad;name/rows")
    assert response.status_code == 400


# === Time Travel ===
def test_list_snapshots(client):
    response = client.get("/api/v1/snapshots?limit=5")
    assert response.status_code == 200
    snapshots = response.json()
    assert 0 < len(snapshots) <= 5
    assert {"snapshot_id", "snapshot_time"} <= set(snapshots[0])


# === SQL sandbox y guardrails ===
def test_sql_query_counts_real_transactions(client):
    response = client.post(
        "/api/v1/query",
        json={"sql": "SELECT COUNT(*) AS n_tx FROM lake.bronze.mssql_bs_transaccion", "limit": 10},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["columns"] == ["n_tx"]
    assert data["rows"][0][0] > 300_000


def test_sql_query_row_cap_is_enforced(client):
    at_cap = client.post(
        "/api/v1/query",
        json={"sql": "SELECT * FROM lake.bronze.mssql_bs_moneda", "limit": 1000},
    )
    assert at_cap.status_code == 200
    assert at_cap.json()["limit_applied"] == 1000

    # Por encima del tope el schema rechaza el pedido (422), no lo recorta en silencio.
    over_cap = client.post(
        "/api/v1/query",
        json={"sql": "SELECT * FROM lake.bronze.mssql_bs_moneda", "limit": 5000},
    )
    assert over_cap.status_code == 422


def test_sql_guardrail_blocks_mutation(client):
    mutations = [
        "DROP TABLE lake.bronze.mssql_bs_moneda",
        "DELETE FROM lake.bronze.mssql_bs_moneda WHERE moneda_id = 1",
        "UPDATE lake.bronze.mssql_bs_moneda SET nombre = 'X'",
        "INSERT INTO lake.bronze.mssql_bs_moneda VALUES (99, 'X', 'X')",
        "ALTER TABLE lake.bronze.mssql_bs_moneda ADD COLUMN foo INT",
        "TRUNCATE lake.bronze.mssql_bs_transaccion",
    ]
    for sql in mutations:
        response = client.post("/api/v1/query", json={"sql": sql})
        assert response.status_code == 400, f"Debería haber bloqueado: {sql}"


def test_sql_guardrail_blocks_multiple_statements(client):
    response = client.post("/api/v1/query", json={"sql": "SELECT 1; SELECT 2"})
    assert response.status_code == 400


# === Removidos por la decisión "solo bronze MSSQL" ===
def test_removed_kpi_endpoint_is_gone(client):
    assert client.get("/api/v1/kpis/monthly-revenue").status_code == 404


# === FastMCP tools y resources ===
def test_mcp_tools_execution():
    inventory = mcp_sqlserver_tables()
    assert {row["table"] for row in inventory} == REAL_TABLES

    tables = mcp_list_tables()
    assert {row["table"] for row in tables} >= REAL_TABLES
    assert {row["schema"] for row in tables} == {"bronze"}

    desc = mcp_describe_table("bronze", "mssql_bs_moneda")
    assert desc["row_count"] > 0
    assert len(desc["sample"]) > 0

    page = mcp_preview_rows("mssql_bs_moneda", limit=3)
    assert page["row_count"] == min(3, page["total_rows"])
    assert len(page["columns"]) == len(page["rows"][0])

    assert mcp_run_query("SELECT 42 AS answer")["rows"] == [[42]]
    assert 0 < len(mcp_list_snapshots(limit=3)) <= 3


def test_mcp_resources_expose_sqlserver_catalog():
    catalog = json.loads(get_sqlserver_catalog_resource())
    assert {row["table"] for row in catalog} == REAL_TABLES
    assert "mssql_bs_transaccion" in get_catalog_resource()
