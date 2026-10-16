# Dulse — PoC Data Lake (DuckLake + DuckDB, 100% open source)

Valida si un lakehouse autohospedado (DuckLake + DuckDB) puede reemplazar el
stack actual Snowflake + Aurora para un volumen < 300 GB. Todo corre en local
con `docker compose`, datos 100% sintéticos, sin credenciales ni datos reales.

OpenSpec: `openspec/changes/poc-data-lake-ducklake/` (proposal, design,
specs, matriz de compatibilidad, tasks).

## Arquitectura

```
postgres-source ──dlt──▶ bronze ──dbt──▶ silver ──dbt──▶ gold ──export──▶ gold_serving (Postgres, BI)
(4 tablas simuladas)    │  DuckLake: catálogo en postgres-catalog + Parquet en Garage (S3)
                        ▼
              orquestado por Prefect (flow dulse-medallion)
              backup: pg_dump + espejo S3 · time travel: AT (VERSION => N)
```

| Componente | Qué es | Dir / archivo |
|---|---|---|
| Infra | `postgres-catalog` :5433, `postgres-source` :5434, Garage S3 :3900–3903, `prefect-server` :4200 | `docker-compose.yml`, `scripts/bootstrap.sh` |
| Origen simulado | DDL 4 tablas + generador sintético (`--scale demo/base/10gb/50gb`) | `source/` |
| Ingesta bronze | Pipeline dlt incremental por `updated_at`, merge/upsert por PK | `bronze/` |
| Modelos | silver (tipado/limpieza/dedup) + 3 gold solo desde silver, 28 tests | `dbt/` |
| Orquestación | Flow `dulse-medallion`: ingesta → `dbt run` → `dbt test` | `orchestration/` |
| Operación | backup/restore, time travel, concurrencia, benchmark, export gold | `scripts/` |
| Hallazgos | limitaciones, riesgos y recomendación de avanzar o no | `docs/findings.md` |

Versiones fijadas (matriz completa en
`openspec/changes/poc-data-lake-ducklake/specs/compat-matrix.md`):
`postgres:17.6-bookworm`, `dxflrs/garage:v2.4.1`,
`prefecthq/prefect:3.6.22-python3.12` (server) / `prefect==3.8.8` (cliente),
`duckdb==1.5.6` (DuckLake 1.0), `dlt==1.31.0`, `dbt-core==1.12.5`,
`dbt-duckdb==1.11.0`.

## Setup desde cero

```bash
cp .env.example .env   # opcional (hay defaults locales)
docker compose up -d
./scripts/bootstrap.sh

# 1. Origen + seed (requiere psycopg: pip install -r source/requirements.txt)
psql "$SOURCE_DSN" -f source/ddl.sql   # o via generate.py (aplica el DDL solo)
SOURCE_DSN=postgresql://source_user:source_password@localhost:5434/source_db \
  python3 source/generate.py --mode seed --scale base --out postgres

# 2. Ingesta bronze (venv propio)
uv venv bronze/.venv --python 3.12
uv pip install -p bronze/.venv/bin/python -r bronze/requirements.txt
bronze/.venv/bin/python bronze/pipeline.py

# 3. Modelos + tests
uv venv dbt/.venv --python 3.12 && uv pip install -p dbt/.venv/bin/python -r dbt/requirements.txt
dbt/.venv/bin/dbt run --project-dir dbt --profiles-dir dbt
dbt/.venv/bin/dbt test --project-dir dbt --profiles-dir dbt   # 28/28

# 4. Orquestación (registra en prefect-server)
uv venv orchestration/.venv --python 3.12 && uv pip install -p orchestration/.venv/bin/python -r orchestration/requirements.txt
PREFECT_API_URL=http://localhost:4200/api orchestration/.venv/bin/python orchestration/flow.py

# 5. Capa de servicio BI (opcional, ver §7.5 en docs/findings.md)
bronze/.venv/bin/python scripts/export_gold.py   # gold -> base gold_serving (Postgres)
```

Operación:

```bash
./scripts/backup.sh [destino]              # dump catálogo + espejo bucket + manifiesto
./scripts/restore.sh <backup>              # recreate + pg_restore + re-subida + verificación
bronze/.venv/bin/python scripts/timetravel_check.py    # 7.2
bronze/.venv/bin/python scripts/concurrency_check.py   # 7.3 (2 lectores + 1 escritor)
bronze/.venv/bin/python scripts/benchmark.py          # 7.4 (JSON con tiempos)
```

## Resultados benchmark (7.4)

Host: 22 CPU, 32 GB RAM, NVMe (200 GB libres); Garage nodo único en el mismo
host; conexiones DuckDB frías. Medido 2026-10-08 (`scripts/benchmark.py`).

| Escala | Clientes | Filas totales | Gen. CSV | Ingesta lago | Throughput | Parquet | Q1 agg mes | Q2 join top-10 | Q3 puntual |
|---|---|---|---|---|---|---|---|---|---|
| demo | 100 | 1 540 | 0,03 s | 0,4 s | 3,9 k filas/s¹ | 0,2 MB | 0,03 s | 0,13 s | 0,04 s |
| base | 10 000 | 155 333 | 1,0 s | 0,5 s | 311 k filas/s | ~5 MB | 0,06 s | 0,14 s | 0,04 s |
| mid | 100 000 | 1 550 469 | 9,5 s | 1,0 s | 1,6 M filas/s | ~52 MB | 0,13 s | 0,17 s | 0,05 s |
| 10gb² | 2 000 000 | ~31 M | ~190 s | ~20 s | — | ~1,1 GB | ~0,3 s | — | — |
| 50gb² | 10 000 000 | ~155 M | ~950 s | ~100 s | — | ~5,3 GB | ~1 s | — | — |

¹ demo dominado por costos fijos (attach/commit), no por throughput.
² **Extrapolado** por regresión lineal desde los 3 puntos medidos (marcado
como tal; no materializado — ver limitaciones en `docs/findings.md`).

Queries: Q1 `GROUP BY mes, moneda` sobre payments; Q2 top-10 clientes por
monto (join payments→leases→customers, últimos 6 meses); Q3 lookup por
`payment_id`. Detalle metodológico y números completos en `docs/findings.md`.

## Estado de validación punta a punta

| Slice | Evidencia |
|---|---|
| Infra + smoke | `bootstrap.sh` idempotente, attach DuckLake canónico |
| Seed + ingesta | demo 100/50/200/1309; incremental trae solo delta; reintento 0 filas |
| dbt | `run` 7/7, `test` 28/28, controles gold == silver OK |
| Prefect | flow `dulse-medallion` Completed; con origen caído Failed sin correr dbt |
| 7.1 backup/restore | destruir (DROP DB + purge 69 objetos) → restore converge 14/14 conteos |
| 7.2 time travel | `AT (VERSION => 144)` devuelve 3 filas previas tras DELETE+UPDATE |
| 7.3 concurrencia | 2×90 lecturas OK + 20 escrituras commit, conteo final exacto |
| 7.4 benchmark | tabla arriba; 10/50 GB extrapolados con metodología documentada |
| 7.5 serving | SÍ exportar: `scripts/export_gold.py` idempotente (81/86/50 filas, sumas = lago) |

Fricciones conocidas (detalle en `docs/findings.md`): catálogo DuckLake
0.3→1.0 exige `duckdb>=1.5.2` en todos los venvs (ya unificado en 1.5.6);
`--full-refresh`/replace en bronze visible a consumidores (reintentos);
server Prefect 3.6.22 vs cliente 3.8.8 (solo warning, runs tracked OK);
`dbt/profiles.yml` con hosts fijos (deuda documentada en
`orchestration/README.md`).
