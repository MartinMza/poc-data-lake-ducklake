"""Dulse Lake FastMCP Server — herramientas para explorar la data cargada desde SQL Server.

El lago contiene únicamente las 7 tablas reales ingestadas desde SQL Server
(`bronze.mssql_bs_*` y `bronze.mssql_d_mart_*`) más los metadatos de dlt.
Todas las herramientas son de solo lectura.
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP

from api import engine

mcp = FastMCP(
    "dulse-datalake",
    instructions=(
        "Servidor MCP para consultar el Data Lake DuckLake. El contenido proviene "
        "íntegramente de SQL Server (esquema bronze: mssql_bs_* y mssql_d_mart_*). "
        "Empezá por 'sqlserver_tables' para ver qué hay cargado y seguí con "
        "'describe_table', 'preview_rows' o 'run_query'."
    ),
)


@mcp.tool()
def sqlserver_tables() -> list[dict[str, Any]]:
    """Inventario de lo que está cargado desde SQL Server.

    Devuelve por tabla: nombre, filas, cantidad de columnas, columnas y el
    `last_load_id` de la corrida de ingesta más reciente (marca de frescura).
    """
    return engine.sqlserver_tables()


@mcp.tool()
def list_tables(schemas: list[str] | None = None) -> list[dict[str, Any]]:
    """Lista todas las tablas del Data Lake con su conteo de filas.

    El lago tiene un solo esquema (`bronze`); se puede pedir otro explícitamente.
    """
    target_schemas = tuple(schemas) if schemas else engine.DEFAULT_SCHEMAS
    return engine.list_tables(schemas=target_schemas)


@mcp.tool()
def describe_table(schema: str, table: str) -> dict[str, Any]:
    """Describe una tabla: columnas, tipos, conteo total y una muestra de 5 filas."""
    return engine.describe_table(schema=schema, table=table)


@mcp.tool()
def preview_rows(
    table: str,
    limit: int = 20,
    offset: int = 0,
    schema: str = "bronze",
) -> dict[str, Any]:
    """Devuelve una página de filas reales de una tabla del lago (máx. 100 por página).

    Pensado para inspeccionar la data de SQL Server sin escribir SQL:
    `preview_rows("mssql_bs_transaccion", limit=10)`.
    """
    return engine.preview_rows(schema=schema, table=table, limit=limit, offset=offset)


@mcp.tool()
def run_query(sql: str, limit: int = 100) -> dict[str, Any]:
    """Ejecuta una consulta SQL analítica de solo lectura sobre el DuckLake.

    Tablas disponibles (esquema `bronze`):
    - `lake.bronze.mssql_bs_moneda`, `lake.bronze.mssql_bs_producto`,
      `lake.bronze.mssql_bs_organizacion`
    - `lake.bronze.mssql_bs_transaccion`, `lake.bronze.mssql_bs_operacion`
    - `lake.bronze.mssql_d_mart_ventas`,
      `lake.bronze.mssql_d_mart_ventas_producto_cliente`
    - Time Travel: `SELECT * FROM lake.bronze.mssql_bs_moneda AT (VERSION => 144)`

    Guardrails:
    - Solo sentencias SELECT, WITH, DESCRIBE, SUMMARIZE, EXPLAIN.
    - DDL/DML (DROP, DELETE, INSERT, UPDATE, etc.) están bloqueados.
    - Sin lectura de archivos ni estado externo: funciones como read_csv/
      read_text/read_parquet/glob/duckdb_secrets y las URLs/rutas con esquema
      '://' (http, https, s3) están bloqueadas.
    - Límite máximo automático de 1000 filas.
    """
    return engine.execute_query(sql=sql, limit=limit)


@mcp.tool()
def list_snapshots(limit: int = 10) -> list[dict[str, Any]]:
    """Lista los snapshots del catálogo DuckLake (auditoría y Time Travel)."""
    return engine.list_snapshots(limit=limit)


@mcp.resource("lake://catalog")
def get_catalog_resource() -> str:
    """Inventario completo de tablas y filas del Data Lake."""
    return json.dumps(engine.list_tables(), indent=2)


@mcp.resource("lake://sqlserver/tables")
def get_sqlserver_catalog_resource() -> str:
    """Inventario de las tablas cargadas desde SQL Server, con marca de última carga."""
    return json.dumps(engine.sqlserver_tables(), indent=2)


if __name__ == "__main__":
    # Permite ejecutar directamente por stdio para Claude Desktop / Cursor
    # (transporte local: no pasa por la autenticación HTTP).
    mcp.run(transport="stdio")
