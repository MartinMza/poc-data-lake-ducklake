"""Suite de pruebas de integración para Dulse Data Lake API y FastMCP Server."""

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.mcp_server import (
    describe_table as mcp_describe_table,
    get_catalog_resource,
    get_kpi as mcp_get_kpi,
    list_snapshots as mcp_list_snapshots,
    list_tables as mcp_list_tables,
    run_query as mcp_run_query,
)


AUTH_HEADERS = {"X-API-Key": "test-key-1"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app, headers=AUTH_HEADERS) as test_client:
        yield test_client


# === Test Suite 1: Healthcheck ===
def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "dulse-lake-api"
    assert data["mcp_mounted"] is True


# === Test Suite 2: Catálogo y Tablas ===
def test_list_tables_all_layers(client):
    response = client.get("/api/v1/tables")
    assert response.status_code == 200
    tables = response.json()
    assert len(tables) >= 15

    schemas = {t["schema"] for t in tables}
    assert "bronze" in schemas
    assert "silver" in schemas
    assert "gold" in schemas

    # Validar presencia de tablas clave de Postgres y MSSQL
    table_names = {t["full_name"] for t in tables}
    assert "bronze.customers" in table_names
    assert "bronze.mssql_bs_transaccion" in table_names
    assert "silver.silver_customers" in table_names
    assert "gold.gold_monthly_revenue" in table_names


def test_describe_existing_table(client):
    response = client.get("/api/v1/tables/gold/gold_monthly_revenue")
    assert response.status_code == 200
    data = response.json()
    assert data["schema"] == "gold"
    assert data["table"] == "gold_monthly_revenue"
    assert data["row_count"] > 0
    assert len(data["columns"]) == 4
    col_names = [c["name"] for c in data["columns"]]
    assert "month" in col_names
    assert "total_amount" in col_names
    assert len(data["sample"]) <= 5


def test_describe_table_not_found(client):
    response = client.get("/api/v1/tables/gold/tabla_inexistente")
    assert response.status_code == 404


def test_describe_table_invalid_identifier(client):
    response = client.get("/api/v1/tables/gold/tabla;drop")
    assert response.status_code == 400


# === Test Suite 3: KPIs Curados ===
def test_kpi_monthly_revenue(client):
    response = client.get("/api/v1/kpis/monthly-revenue")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) > 0
    assert "total_amount" in rows[0]
    assert "month" in rows[0]


def test_kpi_customer_revenue(client):
    response = client.get("/api/v1/kpis/customer-revenue?limit=5")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) <= 5
    assert "customer_id" in rows[0]
    assert "total_paid" in rows[0]


def test_kpi_unit_performance(client):
    response = client.get("/api/v1/kpis/unit-performance?limit=5")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) <= 5
    assert "unit_id" in rows[0]
    assert "total_collected" in rows[0]


def test_kpi_unknown_returns_400(client):
    response = client.get("/api/v1/kpis/kpi_desconocido")
    assert response.status_code == 400


# === Test Suite 4: Snapshots y Time Travel ===
def test_list_snapshots(client):
    response = client.get("/api/v1/snapshots?limit=5")
    assert response.status_code == 200
    snapshots = response.json()
    assert len(snapshots) > 0
    assert "snapshot_id" in snapshots[0]
    assert "snapshot_time" in snapshots[0]


# === Test Suite 5: SQL Sandbox & Guardrails ===
def test_sql_query_valid(client):
    response = client.post(
        "/api/v1/query",
        json={"sql": "SELECT COUNT(*) AS total FROM lake.silver.silver_payments", "limit": 10},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["columns"] == ["total"]
    assert data["rows"][0][0] == 1309


def test_sql_query_mssql_transactions(client):
    response = client.post(
        "/api/v1/query",
        json={"sql": "SELECT COUNT(*) AS n_tx FROM lake.bronze.mssql_bs_transaccion", "limit": 10},
    )
    assert response.status_code == 200
    assert response.json()["rows"][0][0] > 300000


def test_sql_guardrail_blocks_mutation(client):
    mutations = [
        "DROP TABLE lake.gold.gold_monthly_revenue",
        "DELETE FROM lake.silver.silver_payments WHERE id = 1",
        "UPDATE lake.bronze.customers SET name = 'hacked'",
        "INSERT INTO lake.gold.gold_monthly_revenue VALUES ('2026-10-01', 'USD', 100, 1)",
        "ALTER TABLE lake.gold.gold_monthly_revenue ADD COLUMN foo INT",
        "TRUNCATE lake.silver.silver_leases",
    ]
    for sql in mutations:
        response = client.post("/api/v1/query", json={"sql": sql})
        assert response.status_code == 400, f"Debería haber bloqueado: {sql}"


def test_sql_guardrail_blocks_multiple_statements(client):
    response = client.post(
        "/api/v1/query",
        json={"sql": "SELECT 1; SELECT 2"},
    )
    assert response.status_code == 400


# === Test Suite 6: FastMCP Tools & Resources ===
def test_mcp_tools_execution():
    # 1. list_tables
    tables = mcp_list_tables(["gold"])
    assert len(tables) == 3
    assert {t["table"] for t in tables} == {
        "gold_monthly_revenue",
        "gold_customer_revenue",
        "gold_unit_performance",
    }

    # 2. describe_table
    desc = mcp_describe_table("gold", "gold_customer_revenue")
    assert desc["row_count"] > 0
    assert len(desc["sample"]) > 0

    # 3. get_kpi
    rev = mcp_get_kpi("monthly_revenue")
    assert len(rev) > 0

    # 4. run_query
    res = mcp_run_query("SELECT 42 AS answer")
    assert res["rows"] == [[42]]

    # 5. list_snapshots
    snaps = mcp_list_snapshots(limit=3)
    assert len(snaps) > 0

    # 6. get_catalog_resource
    cat_str = get_catalog_resource()
    assert "gold.gold_monthly_revenue" in cat_str
