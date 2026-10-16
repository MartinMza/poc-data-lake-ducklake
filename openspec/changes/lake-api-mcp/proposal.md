## Why

Actualmente los datos del DuckLake (`bronze`, `silver`, `gold`, incluyendo tablas de Postgres y MSSQL) solo pueden consultarse mediante scripts locales de Python o la copia parcial `gold_serving`. Se necesita una capa de servicio unificada que permita tanto a aplicaciones/dashboards (vía HTTP REST) como a agentes de IA (vía Model Context Protocol - MCP) explorar el catálogo completo, consultar métricas curadas y ejecutar consultas analíticas SQL seguras en tiempo real.

## What Changes

- Nuevo módulo `api/` con entorno Python 3.12 (`uv`) basado en **FastAPI** y **FastMCP** sobre `duckdb==1.5.6`.
- Motor de consulta unificado contra el DuckLake (`lake.bronze`, `lake.silver`, `lake.gold`) con soporte de Time Travel y guardrails de seguridad (solo lectura estricto, bloqueo de DDL/DML y límite automático de filas).
- API REST con FastAPI documentada con OpenAPI (`/docs`) para listar tablas, inspeccionar esquemas, obtener KPIs de negocio curados y ejecutar SQL read-only.
- Servidor MCP con FastMCP acoplado al mismo motor (disponible por `stdio` para clientes locales como Claude Desktop/Cursor y montado por HTTP/SSE en FastAPI).

## Capabilities

### New Capabilities
- `lake-query-engine`: Conexión DuckDB al catálogo DuckLake, introspección de esquemas/tablas (`bronze`, `silver`, `gold`), consultas curadas y sandbox SQL read-only con límite de filas.
- `rest-api-serving`: Endpoints HTTP REST con FastAPI para catálogo, KPIs y consultas analíticas SQL.
- `mcp-server-serving`: Herramientas (`tools`) y recursos (`resources`) MCP con FastMCP para consumo por agentes de IA (modos `stdio` y HTTP/SSE).

### Modified Capabilities
- Ninguna (cambio puramente aditivo sobre el lago existente).

## Impact

- Nuevo directorio `api/` con su `requirements.txt`, código de aplicación, tests de humo/validación y `README.md`.
- No modifica esquemas ni datos de `bronze/`, `dbt/`, `orchestration/` ni el catálogo `ducklake_catalog` (acceso estrictamente de lectura).
- Reutiliza las variables de entorno existentes en `.env` (`CATALOG_*`, `GARAGE_S3_*`, `DUCKLAKE_DATA_PATH`).
