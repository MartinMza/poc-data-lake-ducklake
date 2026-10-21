"""Dulse Lake FastMCP Server — Herramientas y recursos MCP para agentes de IA."""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP

from api import engine

mcp = FastMCP(
    "dulse-datalake",
    instructions="Servidor MCP para consultar el Data Lake DuckLake (capas bronze, silver, gold y Time Travel).",
)


@mcp.tool()
def list_tables(schemas: list[str] | None = None) -> list[dict[str, Any]]:
    """Lista todas las tablas disponibles en el Data Lake (bronze, silver, gold) con sus conteos de filas actuales."""
    target_schemas = tuple(schemas) if schemas else ("bronze", "silver", "gold")
    return engine.list_tables(schemas=target_schemas)


@mcp.tool()
def describe_table(schema: str, table: str) -> dict[str, Any]:
    """Describe una tabla del Data Lake: columnas, tipos de datos, conteo total y una muestra de 5 filas."""
    return engine.describe_table(schema=schema, table=table)


@mcp.tool()
def get_kpi(metric: str, limit: int = 20) -> list[dict[str, Any]]:
    """Obtiene métricas curadas de la capa Gold.

    Métricas disponibles:
    - 'monthly_revenue': ingresos mensuales históricos por moneda.
    - 'customer_revenue': top clientes por monto total pagado.
    - 'unit_performance': rendimiento y recaudación por unidad habitacional.
    """
    clean_metric = metric.lower().strip()
    if clean_metric in ("monthly_revenue", "monthly-revenue", "revenue"):
        return engine.get_monthly_revenue()
    if clean_metric in ("customer_revenue", "customer-revenue", "customers"):
        return engine.get_customer_revenue(limit=limit)
    if clean_metric in ("unit_performance", "unit-performance", "units"):
        return engine.get_unit_performance(limit=limit)
    raise ValueError(
        f"Métrica desconocida: '{metric}'. Métricas válidas: 'monthly_revenue', 'customer_revenue', 'unit_performance'."
    )


@mcp.tool()
def run_query(sql: str, limit: int = 100) -> dict[str, Any]:
    """Ejecuta una consulta SQL analítica en modo solo lectura sobre el DuckLake.

    Ejemplos de tablas consultables:
    - `lake.gold.gold_monthly_revenue`
    - `lake.silver.silver_payments`
    - `lake.bronze.mssql_bs_transaccion` (operaciones de MSSQL)
    - Time Travel: `SELECT * FROM lake.gold.gold_monthly_revenue AT (VERSION => 144)`

    Guardrails:
    - Solo sentencias SELECT, WITH, DESCRIBE, EXPLAIN.
    - DDL y DML (DROP, DELETE, INSERT, UPDATE, etc.) están estrictamente bloqueados.
    - Límite máximo automático de 1000 filas.
    """
    return engine.execute_query(sql=sql, limit=limit)


@mcp.tool()
def list_snapshots(limit: int = 10) -> list[dict[str, Any]]:
    """Lista las versiones históricas (snapshots) del catálogo DuckLake para auditoría y Time Travel."""
    return engine.list_snapshots(limit=limit)


@mcp.resource("lake://catalog")
def get_catalog_resource() -> str:
    """Recurso con el inventario completo de tablas y filas del Data Lake."""
    tables = engine.list_tables()
    return json.dumps(tables, indent=2)


if __name__ == "__main__":
    # Permite ejecutar directamente por stdio para Claude Desktop / Cursor
    mcp.run(transport="stdio")
