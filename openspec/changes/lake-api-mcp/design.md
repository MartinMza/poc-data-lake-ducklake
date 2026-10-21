## Context

El DuckLake (`postgres-catalog` :5433 + Garage S3 :3900) almacena tres capas: `bronze` (4 tablas Postgres + 10 tablas MSSQL con >470k filas totales), `silver` (4 modelos limpios de dbt) y `gold` (3 modelos de negocio). Hoy no existe una interfaz de servicio programática ni para aplicaciones web (HTTP REST) ni para agentes de IA (MCP) que permita consultar todo el lago de forma segura.

## Goals / Non-Goals

**Goals:**
- Proveer acceso de lectura a **todo el lago** (`bronze`, `silver`, `gold` y snapshots de Time Travel) desde un único módulo `api/`.
- Exponer una **API REST con FastAPI** (con documentación OpenAPI en `/docs`) y un **servidor MCP con FastMCP** (transporte `stdio` y HTTP/SSE) compartiendo la misma lógica de negocio.
- Soportar tanto **capacidades curadas** (listado de tablas con conteos, descripción de esquema + sample, KPIs de Gold, búsqueda operativa) como **consultas SQL libres read-only** con guardrails.
- Incluir soporte para ejecución en contenedor vía `docker-compose.yml` (`lake-api`) y un script unificado (`scripts/run_api.sh`) para poner todo en marcha con un solo comando.

**Non-Goals:**
- Ejecutar escrituras, ingestas o transformaciones (`INSERT`, `UPDATE`, `DELETE`, `DROP`) desde la API o el MCP.
- Sistema de autenticación/autorización multi-usuario en esta etapa PoC local.

## Decisions

### 1. Motor DuckDB directo contra DuckLake en lugar de leer solo `gold_serving`
La conexión se realiza mediante `duckdb==1.5.6` adjuntando el catálogo `ducklake:postgres:...` como `lake` (mismo patrón probado en `scripts/lake_counts.py`).
- *Alternativa descartada*: Conectar solo a Postgres `gold_serving`. Descartada porque `gold_serving` solo contiene las 3 tablas Gold y el requerimiento exige visibilidad completa de `bronze`, `silver` y `gold` (incluyendo tablas MSSQL y Time Travel `AT (VERSION => N)`).

### 2. Core compartido (`engine.py`) + doble adaptador (FastAPI y FastMCP)
Toda la interacción con DuckLake y las validaciones viven en `api/engine.py`.
- `api/main.py` expone los endpoints HTTP REST con **FastAPI** y monta el servidor SSE/HTTP de **FastMCP**.
- `api/mcp_server.py` define las herramientas (`@mcp.tool`) y recursos (`@mcp.resource`) con **FastMCP**, permitiendo además invocación directa por `stdio` (`api/.venv/bin/python -m api.mcp_server`) para clientes como Claude Desktop, Cursor o Antigravity.
- *Alternativa descartada*: Dos servicios separados con lógica duplicada.

### 3. Guardrails de SQL libre en dos capas (Validación sintáctica + Row Cap)
Para proteger el lago en el endpoint/tool de SQL libre (`run_query`):
1. **Validación Read-Only**: Solo se permiten sentencias que comiencen con `SELECT` o `WITH` (más introspección `DESCRIBE`/`SUMMARIZE`/`EXPLAIN`). Se rechaza cualquier sentencia con múltiples statements (`;`) o keywords de mutación/sistema (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `REPLACE`, `MERGE`, `COPY`, `ATTACH`, `DETACH`, `INSTALL`, `LOAD`, `PRAGMA`, `CALL`, `SET`).
2. **Row Limiting**: Se aplica un tope de filas (default `100`, máximo `1000`) envolviendo o limitando el resultado (`fetchmany(limit)`) para evitar saturar memoria o el contexto del agente al consultar tablas de >320k filas como `bronze.mssql_bs_transaccion`.

### 4. Empaquetado dual: Docker Compose (`lake-api`) + Script de arranque (`scripts/run_api.sh`)
- `api/Dockerfile`: imagen Python 3.12 con dependencias para correr la API en red interna de Docker (`postgres-catalog:5432`, `garage:3900`).
- `docker-compose.yml`: servicio opcional `lake-api` mapeando `8000:8000` con healthcheck en `/health`.
- `scripts/run_api.sh`: script que levanta el stack (`docker compose up -d`), verifica salud y arranca la API en host o en contenedor según flag (`--docker` o `--host`).

## Risks / Trade-offs

- [Divergencia de versión DuckLake] → Mitigación: fijar `duckdb==1.5.6` en `api/requirements.txt`, alineado con `bronze/`, `dbt/` y `orchestration/`.
- [Consultas ad-hoc costosas en `bronze`] → Mitigación: límite estricto de filas devueltas (`<= 1000`) y serialización JSON segura de tipos `Decimal`, `date` y `datetime`.
- [Concurrencia de conexiones DuckDB en servidor web] → Mitigación: usar conexiones de corta duración por consulta o lock sobre la instancia para evitar conflictos entre hilos de FastAPI/MCP.

## Migration Plan

Cambio 100% aditivo en `api/`:
1. Crear `api/.venv` con Python 3.12 e instalar `api/requirements.txt` (`fastapi`, `uvicorn`, `fastmcp`, `duckdb==1.5.6`).
2. Verificar endpoints REST y tools MCP contra el DuckLake activo.
3. Rollback: detener el proceso y eliminar `api/` (o remover `lake-api` del compose).

## Open Questions

- Ninguna bloqueante para el PoC; el servicio corre sobre el host con los mismos defaults de `.env` (`localhost:5433` y `localhost:3900`) y en docker con los hostnames internos.
