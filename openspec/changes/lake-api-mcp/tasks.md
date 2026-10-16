## 1. Entorno y Motor de Consulta DuckLake (`lake-query-engine`)

- [ ] 1.1 Crear estructura `api/` con `requirements.txt` (`fastapi`, `uvicorn`, `fastmcp`, `duckdb==1.5.6`, `httpx`, `pytest`) y entorno virtual `api/.venv` (Python 3.12 via `uv`)
- [ ] 1.2 Implementar `api/engine.py` con conexión al DuckLake (`bronze`, `silver`, `gold`), introspección de catálogo (`list_tables`, `describe_table`, `list_snapshots`), consultas curadas de KPIs y ejecutor SQL con guardrails read-only y row cap (`limit <= 1000`)

## 2. Interfaces FastAPI y FastMCP (`rest-api-serving` & `mcp-server-serving`)

- [ ] 2.1 Implementar `api/mcp_server.py` usando **FastMCP** con tools (`list_tables`, `describe_table`, `get_kpi`, `run_query`, `list_snapshots`) y resource de catálogo (`lake://catalog`), soportando ejecución `stdio` y montaje ASGI
- [ ] 2.2 Implementar `api/main.py` usando **FastAPI** con endpoints REST (`/health`, `/api/v1/tables`, `/api/v1/tables/{schema}/{table}`, `/api/v1/kpis/{kpi_name}`, `/api/v1/snapshots`, `POST /api/v1/query`), OpenAPI en `/docs` y montaje del servidor FastMCP

## 3. Validación y Documentación

- [ ] 3.1 Crear y ejecutar suite de validación `api/test_api_mcp.py` contra el DuckLake real verificando catálogo (bronze PG/MSSQL + silver + gold), KPIs curados, queries SQL read-only, Time Travel, bloqueo de DDL/DML (403) y llamadas a tools de FastMCP
- [ ] 3.2 Documentar en `api/README.md` cómo levantar la API FastAPI, cómo conectar clientes MCP (`stdio` y HTTP/SSE) y ejemplos de uso
