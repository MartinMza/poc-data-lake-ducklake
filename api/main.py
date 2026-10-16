"""Dulse Data Lake — FastAPI Application con endpoints REST y montaje FastMCP."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from api import engine
from api.mcp_server import mcp

app = FastAPI(
    title="Dulse Data Lake API & MCP",
    version="1.0.0",
    description="API REST y servidor MCP para consultar el DuckLake (capas bronze, silver, gold y Time Travel).",
)

# Habilitar CORS para integración con dashboards web / frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Montar servidor FastMCP con transporte SSE bajo /mcp
app.mount("/mcp", mcp.http_app(transport="sse"))


class QueryRequest(BaseModel):
    sql: str = Field(
        ...,
        description="Consulta SQL de solo lectura (SELECT, WITH, DESCRIBE, EXPLAIN).",
        examples=["SELECT * FROM lake.gold.gold_monthly_revenue LIMIT 5"],
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Cantidad máxima de filas a retornar (1 a 1000).",
    )


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    limit_applied: int
    sql_executed: str


@app.get("/", include_in_schema=False)
def root():
    """Redirige automáticamente a la documentación interactiva Swagger UI."""
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["Health"])
def health_check():
    """Verifica el estado del servicio y conectividad básica."""
    return {
        "status": "healthy",
        "service": "dulse-lake-api",
        "version": "1.0.0",
        "mcp_mounted": True,
    }


@app.get("/api/v1/tables", tags=["Catálogo"])
def list_lake_tables(
    schemas: str = Query(
        default="bronze,silver,gold",
        description="Esquemas separados por comas a incluir.",
    )
):
    """Lista las tablas del Data Lake con sus conteos de filas actuales."""
    schema_list = tuple(s.strip() for s in schemas.split(",") if s.strip())
    try:
        return engine.list_tables(schemas=schema_list)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error consultando tablas del lago: {exc}",
        ) from exc


@app.get("/api/v1/tables/{schema}/{table}", tags=["Catálogo"])
def describe_lake_table(schema: str, table: str):
    """Describe las columnas, tipos de datos y muestra de 5 filas de una tabla."""
    try:
        return engine.describe_table(schema=schema, table=table)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error describiendo tabla: {exc}",
        ) from exc


@app.get("/api/v1/kpis/{kpi_name}", tags=["KPIs Curados"])
def get_curated_kpi(
    kpi_name: str,
    currency: str | None = Query(
        default=None, description="Moneda para filtrar (ej. USD, UYU)."
    ),
    limit: int = Query(
        default=20, ge=1, le=500, description="Límite de registros para tops."
    ),
):
    """Obtiene métricas pre-calculadas de la capa Gold.

    KPIs soportados:
    - `monthly-revenue`: ingresos mensuales agregados.
    - `customer-revenue`: clientes ordenados por recaudación total.
    - `unit-performance`: desempeño y recaudación por unidad.
    """
    clean_name = kpi_name.lower().replace("-", "_").strip()
    try:
        if clean_name in ("monthly_revenue", "revenue"):
            return engine.get_monthly_revenue(currency=currency)
        if clean_name in ("customer_revenue", "customers"):
            return engine.get_customer_revenue(limit=limit)
        if clean_name in ("unit_performance", "units"):
            return engine.get_unit_performance(limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error consultando KPI '{kpi_name}': {exc}",
        ) from exc

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"KPI '{kpi_name}' no soportado. Disponibles: 'monthly-revenue', 'customer-revenue', 'unit-performance'.",
    )


@app.get("/api/v1/snapshots", tags=["Time Travel"])
def list_snapshots(
    limit: int = Query(
        default=10, ge=1, le=100, description="Cantidad de snapshots a listar."
    )
):
    """Lista los snapshots históricos del DuckLake para auditoría y Time Travel."""
    try:
        return engine.list_snapshots(limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error obteniendo snapshots de DuckLake: {exc}",
        ) from exc


@app.post(
    "/api/v1/query",
    response_model=QueryResponse,
    tags=["SQL Sandbox"],
)
def run_sql_query(payload: QueryRequest):
    """Ejecuta una consulta SQL analítica en modo solo lectura con guardrails."""
    try:
        res = engine.execute_query(sql=payload.sql, limit=payload.limit)
        return res
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error de ejecución SQL: {exc}",
        ) from exc
