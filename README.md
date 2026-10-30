# KavosLake — PoC Data Lake (DuckLake + DuckDB, 100% open source)

Valida si un lakehouse autohospedado (DuckLake + DuckDB) puede reemplazar el
stack actual Snowflake + Aurora para un volumen < 300 GB. Todo corre en local
con `docker compose`.

**Estado actual**: la única fuente de datos es **SQL Server** (backup `.bak`
real restaurado en el contenedor `mssql`). El origen Postgres sintético, las
capas dbt (`silver`/`gold`) y la capa de serving fueron retirados: el lago
contiene solo las 7 tablas ingestadas desde SQL Server.

OpenSpec: `openspec/changes/` (proposals, diseños, specs y tasks históricos).

**¿Primera vez acá, o volvés después de un tiempo?** Leé
[`docs/MANUAL.md`](docs/MANUAL.md): manual corto, por bloques, con el camino
feliz, los comandos que se usan de verdad y una tabla de fallas comunes.

## Arquitectura

```
SQL Server (.bak) ──restore──▶ mssql ──bcp/MERGE (fast) o dlt──▶ bronze ──▶ API REST + MCP
                                                               │  DuckLake: catálogo en postgres-catalog + Parquet en Garage (S3)
                                                               ▼
                                              orquestado por Prefect (flow kavos_lake-medallion)
                                              backup: pg_dump + espejo S3 · time travel: AT (VERSION => N)
```

| Componente | Qué es | Dir / archivo |
|---|---|---|
| Infra | `postgres-catalog` :5433, Garage S3 :3900–3903, `prefect-server` :4200, `mssql` :1433 | `docker-compose.yml`, `scripts/bootstrap.sh` |
| Origen real | `.bak` de SQL Server restaurado (7 tablas del subset: `BSMoneda`, `BSProducto`, `BSOrganizacion`, `BSTransaccion`, `BSOperacion`, `DMart_Ventas`, `DMart_VentasProductoCliente`) | `mssql-backup/` (gitignored), `scripts/restore_mssql.sh` |
| Ingesta bronze | `bronze/fast_mssql.py` (principal: bcp → staging → MERGE) y `bronze/pipeline_mssql.py` (fallback dlt) | `bronze/` |
| Purga de data de ejemplo | deja solo las 7 tablas reales (dry-run por defecto) | `scripts/purge_sample_data.py` |
| Orquestación | Flow `kavos_lake-medallion`: restore condicional → ingesta MSSQL | `orchestration/` |
| Servicio | API REST (FastAPI) + MCP (FastMCP) con auth obligatoria | `src/kavos_lake/` |
| Operación | backup/restore del lago, time travel, conteos | `scripts/` |

Layout del código, puertos/adaptadores y regla de dependencia:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

Versiones fijadas: `postgres:17.6-bookworm`, `dxflrs/garage:v2.4.1`,
`prefecthq/prefect:3.6.22-python3.12` (server) / `prefect==3.8.8` (cliente),
`duckdb==1.5.6` (DuckLake 1.0), `dlt==1.31.0`,
`kavos_lake-mssql:2022-CU27`.

## Setup

Un solo comando idempotente (crea `.env`, levanta contenedores, corre bootstrap,
prepara venvs, restaura el `.bak` si lo hay, ingesta y valida la API/MCP):

```bash
./scripts/setup.sh                  # --scale/--force-seed ya no aplican
```

Paso a paso manual:

```bash
cp .env.example .env   # completar AUTH_API_KEYS / AUTH_USERS / AUTH_JWT_SECRET
docker compose up -d
./scripts/bootstrap.sh

# 1. Restaurar el .bak de SQL Server (idempotente, REPLACE)
./scripts/restore_mssql.sh FAF12_Kavos_2026_10_07_233301_411720.bak FAF12_Kavos

# 2. Ingesta bronze (venv propio)
uv venv bronze/.venv --python 3.12
uv pip install -p bronze/.venv/bin/python -r bronze/requirements.txt
MSSQL_DATABASE=FAF12_Kavos bronze/.venv/bin/python bronze/fast_mssql.py all --full

# 3. Orquestación (registra en prefect-server)
uv venv orchestration/.venv --python 3.12
uv pip install -p orchestration/.venv/bin/python -r orchestration/requirements.txt
PREFECT_API_URL=http://localhost:4200/api MSSQL_DATABASE=FAF12_Kavos \
  orchestration/.venv/bin/python orchestration/flow.py

# 4. API + MCP
uv venv .venv --python 3.12
uv pip install -p .venv/bin/python -r requirements.txt
./scripts/run_api.sh
```

Limpieza de la data de ejemplo (si el lago viene de una corrida anterior):

```bash
bronze/.venv/bin/python scripts/purge_sample_data.py            # plan (dry-run)
bronze/.venv/bin/python scripts/purge_sample_data.py --apply    # aplica
```

Operación:

```bash
./scripts/backup.sh [destino]              # dump catálogo + espejo bucket + manifiesto
./scripts/restore.sh <backup>              # recreate + pg_restore + re-subida + verificación
bronze/.venv/bin/python scripts/timetravel_check.py
bronze/.venv/bin/python scripts/lake_counts.py
```

## API REST + MCP y autenticación

Ambas interfaces exigen credenciales (API key `X-API-Key` o JWT
`Authorization: Bearer`, emitido por `POST /auth/token`). El servicio es
fail-closed: sin `AUTH_API_KEYS`/`AUTH_USERS` no arranca.

```bash
curl -s -H "X-API-Key: $AUTH_API_KEYS" http://localhost:8000/api/v1/sqlserver/tables
curl -s -H "X-API-Key: $AUTH_API_KEYS" \
  "http://localhost:8000/api/v1/sqlserver/tables/mssql_bs_transaccion/rows?limit=5"
```

MCP: `sqlserver_tables`, `list_tables`, `describe_table`, `preview_rows`,
`run_query`, `list_snapshots`, más los recursos `lake://catalog` y
`lake://sqlserver/tables`. Detalle, configuración de clientes y ejemplos en
`docs/API.md`.

## Data disponible (subset real de SQL Server)

| Tabla origen | Filas | Bronze |
|---|---|---|
| `BSMoneda` | 17 | `bronze.mssql_bs_moneda` |
| `BSProducto` | 9 342 | `bronze.mssql_bs_producto` |
| `BSOrganizacion` | 3 055 | `bronze.mssql_bs_organizacion` |
| `BSTransaccion` | 320 447 | `bronze.mssql_bs_transaccion` |
| `BSOperacion` | 148 356 | `bronze.mssql_bs_operacion` |
| `DMart_Ventas` | 275 | `bronze.mssql_d_mart_ventas` |
| `DMart_VentasProductoCliente` | 4 065 | `bronze.mssql_d_mart_ventas_producto_cliente` |

Total: 485 557 filas. `merge` por PK en las 5 tablas con PK y `replace` en las
2 DMart. El fast path normaliza los nombres de columna a `snake_case`.

## Resultados históricos del PoC (data sintética, ya retirada)

Medidos 2026-10-08 con el generador sintético (22 CPU, 32 GB RAM, NVMe; Garage
nodo único; conexiones DuckDB frías). Se conservan como evidencia del
rendimiento del formato; el generador y los scripts de benchmark/concurrencia
fueron eliminados al retirar la data de ejemplo.

| Escala | Clientes | Filas totales | Ingesta lago | Throughput | Parquet | Q1 agg mes | Q2 join top-10 | Q3 puntual |
|---|---|---|---|---|---|---|---|---|
| demo | 100 | 1 540 | 0,4 s | 3,9 k filas/s¹ | 0,2 MB | 0,03 s | 0,13 s | 0,04 s |
| base | 10 000 | 155 333 | 0,5 s | 311 k filas/s | ~5 MB | 0,06 s | 0,14 s | 0,04 s |
| mid | 100 000 | 1 550 469 | 1,0 s | 1,6 M filas/s | ~52 MB | 0,13 s | 0,17 s | 0,05 s |
| 10gb² | 2 000 000 | ~31 M | ~20 s | — | ~1,1 GB | ~0,3 s | — | — |
| 50gb² | 10 000 000 | ~155 M | ~100 s | — | ~5,3 GB | ~1 s | — | — |

¹ dominado por costos fijos (attach/commit). ² extrapolado por regresión
lineal, no materializado. Lectura clave: 50 GB nominales ≈ 5,3 GB de Parquet
(compresión ~0,33x), así que 300 GB entran en un nodo.

## Validación vigente

| Slice | Evidencia |
|---|---|
| Auth | `tests/test_auth.py` 14 tests (key, JWT, expiración, firma inválida, `/health` público, fail-closed) |
| API/MCP sobre data real | `tests/test_api_mcp.py` 16 tests (bronze-only, inventario SQL Server, paginado, guardrails, tools/recursos) |
| Ingesta MSSQL | 7 tablas, 485 557 filas, paridad fast vs dlt verificada (`bronze/fast_ground_truth.json`) |
| Purga de data de ejemplo | `purge_sample_data.py --apply` + re-ejecución idempotente ("nada que purgar") |
| Backup/restore del lago | dump + espejo + manifold verificado (14/14 conteos en el ciclo histórico) |
| Time travel | `AT (VERSION => N)` sobre tablas propias del checkscript |

Fricciones conocidas: el catálogo DuckLake es estado compartido y exige
`duckdb>=1.5.2` en todos los venvs; `MSSQL_BAK_PATH` debe reapuntarse al
`.bak` del día (nombre rotado); `dbt`/silver/gold ya no forman parte del
stack; Prefect server 3.6.22 vs cliente 3.8.8 solo emite un warning.
