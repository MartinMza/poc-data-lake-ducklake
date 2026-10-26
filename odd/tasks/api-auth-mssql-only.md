# Auth para API/MCP + dejar únicamente la data de SQL Server

Feature `api-auth-mssql-only`: autenticación (API key + JWT) en la API REST y el MCP,
y eliminación completa de la data/código sintético dejando solo las 7 tablas reales
de SQL Server expuestas por la API/MCP.

## Specs

- **S1** (auth): "Agregar motodo de auth de alguna forma para la api y para el MCP"
- **S2** (mecanismo, decisión): "Ambos: API key + JWT"
- **S3** (alcance de datos): "Eliminar la data de ejemplo y quiero que quede solo lo que se carga de SQLServer. Tiene que haber alguna forma de que del MCP poder ver la data que hay caragada del SQLServer."
- **S4** (alcance del borrado, decisión): "Datos + código sintético + tablas de prueba MSSQL"
- **S5** (dbt/KPIs, decisión): "Eliminar dbt del flujo: solo bronze MSSQL expuesto"
- **S6** (vista MCP, decisión): "Catálogo + tabla SQL Server dedicada"

## Tasks

| ID | Spec | Qué | Route | Commit |
|---|---|---|---|---|
| T1 | S1,S2 | Módulo `api/auth.py`: API keys por env (comparación constant-time), JWT HS256 (`POST /auth/token`), usuarios plain/PBKDF2, fail-closed sin credenciales | inline | C1 |
| T2 | S1,S2 | Middleware ASGI de auth aplicado a la API REST y al mount `/mcp`; públicos solo `/health`, `/`, `/docs`, `/openapi.json`, `/auth/token` | inline | C1 |
| T3 | S1,S2 | `POST /auth/token`, env vars en `.env.example` + compose `lake-api`, `scripts/run_api.sh`, helper `scripts/hash_password.py` | inline | C1 |
| T4 | S1,S2 | Tests de auth: sin credencial 401, key inválida 401, key válida 200, token válido, credenciales malas 401, token expirado 401, `/health` público | inline | C1 |
| T5 | S3,S5,S6 | `api/engine.py`: esquemas default = `bronze`; quitar KPIs gold; `sqlserver_tables()` (filas, columnas, `_dlt_load_id`, excluye `_dlt_*`) y `preview_rows(offset,limit)` | inline | C2 |
| T6 | S3,S6 | REST: `/api/v1/sqlserver/tables`, `/api/v1/sqlserver/tables/{table}/rows`; quitar `/api/v1/kpis` | inline | C2 |
| T7 | S3,S6 | MCP: tools `sqlserver_tables`, `preview_rows`; actualizar `list_tables`/`describe_table`/`run_query`/resource a bronze-only | inline | C2 |
| T8 | S3,S4,S5 | `scripts/purge_sample_data.py` (dry-run default, `--apply`): drop tablas sintéticas + `mssql_productos/ventas/movimientos`, esquemas `silver`/`gold`, estado dlt de los pipelines eliminados | inline | C3 |
| T9 | S4 | Borrar `source/`, `bronze/pipeline.py`, `dbt/`, `scripts/export_gold.py`, `scripts/benchmark.py`, `scripts/concurrency_check.py` | worker | C3 |
| T10 | S5 | `orchestration/flow.py`: `bak-watch-restore` → `mssql-ingest` (sin rama postgres ni dbt) | worker | C3 |
| T11 | S3,S4 | `scripts/container_init.sh` y `scripts/setup.sh` sin seed sintético / dbt / export gold; `bronze/*mssql*` con default = 7 tablas reales y `MSSQL_DATABASE=FAF12_Kavos` | worker | C3 |
| T12 | S4 | `docker-compose.yml`: quitar `postgres-source` y `SEED_SCALE`, `lake-init` sin seed; `lake-api` con env de auth | inline | C3 |
| T13 | S1-S6 | Docs: `README.md`, `api/README.md`, `bronze/README.md`, `orchestration/README.md`, `.env.example`, nota de deprecación en `docs/findings*.md` | inline | C4 |
| T14 | S1-S6 | Verificación: `pytest api/`, purga idempotente, flow `nl`, auth por `curl`, conteo final del lago | inline | — |

## Log

- **L1** (pedido original, textual): "Necesito hacer los siguientes cambios.
  1- Agregar motodo de auth de alguna forma para la api y para el MCP
  2- Eliminar la data de ejemplo y quiero que quede solo lo que se carga de SQLServer. Tiene que haber alguna forma de que del MCP poder ver la data que hay caragada del SQLServer."
- **L2** (decisión auth): "Ambos: API key + JWT"
- **L3** (decisión alcance de borrado): "Datos + código sintético + tablas de prueba MSSQL"
- **L4** (decisión dbt/KPIs): "Eliminar dbt del flujo: solo bronze MSSQL expuesto"
- **L5** (decisión vista MCP): "Catálogo + tabla SQL Server dedicada"
