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

## Commits (work units)

- `cefdbca` **C1 — auth**: `api/auth.py`, `api/config.py`, middleware ASGI global
  (REST + `/mcp`), `POST /auth/token`, `api/test_auth.py` (14 tests), helper de
  hash, env/compose del `lake-api`, preflight en `run_api.sh`.
- `b72c0d6` **C2 — API/MCP solo MSSQL**: engine bronze-only +
  `sqlserver_tables()`/`preview_rows()`, endpoints `/api/v1/sqlserver/*`, tools y
  recursos MCP, `api/test_api_mcp.py` reescrito (16 tests), `purge_sample_data.py`.
  Purga aplicada al lago (backup previo en `backups/pre-purge-20261026-175304`).
- **C3 — limpieza de código/infra** (borra `source/`, `bronze/pipeline.py`, `dbt/`,
  scripts de gold/benchmark, reconecta `orchestration/flow.py`,
  `container_init.sh`, `setup.sh`, defaults MSSQL, compose sin `postgres-source`).
- **C4 — documentación** (`README.md`, `api/README.md`, `bronze/README.md`,
  `orchestration/README.md`, notas de vigencia en `docs/findings*.md`).

## Estado de ejecución

| Task | Estado | Evidencia |
|---|---|---|
| T1-T3 | hecho (C1) | `api/auth.py`, middleware global, `/auth/token`; curl contra uvicorn real: `/health` 200, `/api/v1/tables` 401, `/mcp/sse` 401, key válida 200, JWT 200 |
| T4 | hecho (C1) | `api/test_auth.py` 14/14 |
| T5-T7 | hecho (C2) | `api/test_api_mcp.py` 16/16; inventario devuelve las 7 tablas |
| T8 | hecho (C2) | `purge_sample_data.py --apply` + re-run "Nada que purgar"; backup previo `backups/pre-purge-20261026-175304` |
| T9-T11 | hecho (C3) | `source/`, `bronze/pipeline.py`, `dbt/`, `export_gold.py`, `benchmark.py`, `concurrency_check.py` borrados; `flow.py` = restore→ingesta; defaults MSSQL = 7 tablas y `FAF12_Kavos`; `bash -n` + `py_compile` + `flow --help` OK |
| T12 | hecho (C3) | compose sin `postgres-source`/`SEED_SCALE`; `lake-init` sin seed; `lake-api` con `AUTH_*`; `docker compose config` OK |
| T13 | hecho (C4) | README raíz, `api/README`, `bronze/README`, `orchestration/README`, `.env.example`, notas de vigencia en `docs/findings*.md` |
| T14 | parcial | `pytest api/` 30/30, purga idempotente, auth por curl, `flow --help`; falta cerrar con la corrida del flow y el review nativo |

Notas de ejecución:
- La delegación al writer quedó bloqueada por el runtime (exige `Spec:` y luego
  se colgó a los 4 min); el trabajo se completó inline, salvo los cambios de
  `bronze/*.py` que el writer sí alcanzó a dejar y fueron revisados aquí.
- Las bases de prueba de SQL Server (`RestoredProof`, `TestBakSource`) se
  dropearon: en la instancia queda solo `FAF12_Kavos`.

## Review nativo (RDD)

Candidatos revisados como work-unit commits, con `baseRef` + `committedOnly`
(el rango completo de 4 commits excede el presupuesto de contexto del provider:
`lens_context_budget_exceeded`, sin autoridad creada).

| Slice | Lineage | Veredicto | Hallazgos |
|---|---|---|---|
| Docs (`053fe23`) | `review-0652740d3478315e` | approved, quemado | ninguno |
| Auth (`8339c8f`) | `review-d6cc0681bca63e4b` → `review-aaa23e1a6020f34e` | approved, quemado | 3 corregidos + 5 avisos informacionales |
| Limpieza (`fb41cc9`) | — | **pendiente** | — |
| API/MCP (`15812b1`) | — | **pendiente** (cubierto por 16 tests) | — |

Hallazgos que abrieron corrección (los tres reales, los tres corregidos):

1. `R1-secret-example-credentials` (CRITICAL): `.env.example` traía credenciales
   usables + el `AUTH_JWT_SECRET`; copiarlo a `.env` dejaba la auth bypasseable.
   Fix: plantilla vacía + generación en `setup.sh`.
2. `R3-unicode-compare-500` (BLOCKER): `hmac.compare_digest` con `str` no-ASCII
   lanzaba `TypeError` (500 en vez de 401). Fix: comparación sobre bytes UTF-8.
3. `R3-setup-auth-persist` (BLOCKER): el `sed` sólo reescribía una línea
   existente, así que un `.env` previo quedaba sin credencial pese al mensaje de
   éxito. Fix: `set_env_value` (append si falta) + verificación que falla ruidosa.

Avisos informacionales no bloqueantes: `api/main.py:89`, `api/auth.py:154`,
`api/auth.py:115`, `scripts/run_api.sh:43`, `scripts/hash_password.py:27`.

Nota de herramienta: la recovery `scope_changed` no ejecuta por el facade
(lee STATUS sin los selectores `baseRef`/`committedOnly`); se corrió el comando
nativo renderizado por el provider.
