## 1. Entorno y Motor de Consulta DuckLake (`lake-query-engine`)

- [x] 1.1 Crear estructura `api/` con `requirements.txt` (`fastapi`, `uvicorn`, `fastmcp`, `duckdb==1.5.6`, `httpx`, `pytest`) y entorno virtual `api/.venv` (Python 3.12 via `uv`)
- [x] 1.2 Implementar `api/engine.py` con conexión al DuckLake (`bronze`, `silver`, `gold`), introspección de catálogo (`list_tables`, `describe_table`, `list_snapshots`), consultas curadas de KPIs y ejecutor SQL con guardrails read-only y row cap (`limit <= 1000`)

## 2. Interfaces FastAPI, FastMCP y Docker (`rest-api-serving` & `mcp-server-serving`)

- [x] 2.1 Implementar `api/mcp_server.py` usando **FastMCP** con tools (`list_tables`, `describe_table`, `get_kpi`, `run_query`, `list_snapshots`) y resource de catálogo (`lake://catalog`), soportando ejecución `stdio` y montaje ASGI
- [x] 2.2 Implementar `api/main.py` usando **FastAPI** con endpoints REST (`/health`, `/api/v1/tables`, `/api/v1/tables/{schema}/{table}`, `/api/v1/kpis/{kpi_name}`, `/api/v1/snapshots`, `POST /api/v1/query`), OpenAPI en `/docs` y montaje del servidor FastMCP
- [x] 2.3 Crear `api/Dockerfile` y agregar servicio `lake-api` en `docker-compose.yml` (puerto 8000, healthcheck y dependencias de salud)
- [x] 2.4 Crear script `scripts/run_api.sh` ejecutable con soporte `--docker` o `--host` (prepara el entorno, asegura el stack arriba y arranca la API/MCP mostrando endpoints)

## 3. Validación y Documentación

- [x] 3.1 Crear y ejecutar suite de validación `api/test_api_mcp.py` contra el DuckLake real verificando catálogo (bronze PG/MSSQL + silver + gold), KPIs curados, queries SQL read-only, Time Travel, bloqueo de DDL/DML (403) y llamadas a tools de FastMCP
- [x] 3.2 Documentar en `api/README.md` cómo levantar la API FastAPI, cómo conectar clientes MCP (`stdio` y HTTP/SSE) y ejemplos de uso
