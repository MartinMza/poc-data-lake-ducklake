# Matriz de compatibilidad verify-first (tasks 1.1–1.3)

Fecha de consulta: 2026-10-08. Todo lo marcado ✅ fue verificado con ejecución
local (venv Python 3.12, postgres:18 en docker, datos sintéticos de 4 filas).
Lo marcado 📄 está respaldado por documentación oficial vigente (enlaces abajo).

## 1.1 dlt + DuckLake → SOPORTADO ✅ (destino nativo)

- Existe destino nativo `ducklake` desde dlt **1.17.0**; evaluado **dlt 1.31.0**
  (última en PyPI al 2026-10-08, confirmada con `pip index versions`).
- Instalación: `pip install "dlt[ducklake,s3,postgres,sql-database]==1.31.0"`
  (`ducklake` extra exige `duckdb>=1.2.0` + `pyarrow>=16`; `s3` extra aporta
  `s3fs`/`botocore` para el storage; `sql-database` extra aporta
  `sqlalchemy>=1.4` para leer `postgres-source`; `postgres` extra aporta
  `psycopg2-binary`. Rangos leídos del METADATA de los wheels descargados.)
- Combinación del PoC (catálogo Postgres + DATA_PATH en S3/filesystem) es la
  configuración documentada de referencia; Postgres es "el único catálogo
  production-grade con paralelismo total".
- Verificado ✅: pipeline `sql_table` (postgres) → `ducklake` con
  `incremental("updated_at")` + `merge/upsert` por PK contra catálogo
  Postgres real: carga inicial 3 filas, segunda corrida sin cambios carga 0,
  tras INSERT+UPDATE solo trae el delta y aplica upsert sin duplicados
  (4 filas finales correctas). State sync y `pipeline.dataset()`/`sql_client()`
  funcionan sobre el destino.
- Gotchas verificados (no bloqueantes, a respetar en el grupo 4):
  1. El `catalog` debe ser string `postgresql://...` **sin** sufijo de driver
     (`postgresql+psycopg2://` falla con `NotImplementedError`); el sufijo
     `+psycopg2` solo va en la URL del **origen** `sql_table` (sqlalchemy).
  2. El `storage` en código debe pasarse anidado
     (`{"bucket_url": "s3://..."}`); la forma plana como string se ignora en
     silencio y usa el default `<ducklake_name>.files` relativo al cwd.
  3. Usar DATA_PATH absoluto o `s3://` (el default relativo ata el lake al cwd).
  4. `initial_value` del incremental debe ser `datetime`, no string.
  5. La doc de dlt declara "dbt support: Not supported" para este destino
     (el runner dbt integrado de dlt no hereda el ATTACH); irrelevante para el
     PoC porque dbt se orquesta por separado con dbt-duckdb.
- Workaround filesystem+registro manual: **no necesario** (plan B si el destino
  nativo fallara; queda descartado por la verificación).

## 1.2 dbt-duckdb + DuckLake → SOPORTADO ✅ (attach en profiles.yml)

- Soportado vía bloque `attach` (disponible desde dbt-duckdb 1.4.0) con path
  `ducklake:...`; evaluado **dbt-duckdb 1.11.0** (última 1.x en PyPI) +
  **dbt-core 1.12.5** (el adapter exige `dbt-core>=1.8.0`, `duckdb>=1.0.0;
  rangos leídos del METADATA del wheel).
- Verificado ✅ con `profiles.yml` real contra el mismo catálogo Postgres del
  punto 1.1: `dbt debug` en verde, `dbt run` de un modelo con
  `config(database="lake", schema="silver")` crea la tabla **dentro del
  DuckLake**, `dbt test` (`not_null`, `unique`) pasa, segunda corrida de `run`
  idempotente, y la tabla es legible desde una conexión DuckDB independiente.
  Valida el diseño "un solo DuckLake, tres esquemas".
- Perfil mínimo que funciona (contraseña del catálogo Postgres vía string
  conninfo; `METADATA_SCHEMA` = `ducklake_name` usado por dlt):
  `attach: [{path: "ducklake:postgres:host=... dbname=... user=... password=... port=...>", alias: lake, options: {METADATA_SCHEMA: <lake>}}]`
  más `extensions: [ducklake]`.
- Caveats verificados (no bloqueantes, a resolver en el grupo 5):
  1. Sin macro propia, el esquema del modelo es `<profile_schema>_<custom>`
     (observado `main_silver`); para `silver`/`gold` exactos definir
     `generate_schema_name` en el proyecto.
  2. Queda pendiente el attach equivalente con DATA_PATH `s3://` (Garage) +
     secret S3 con `endpoint_url` custom; es la única pata sin smoke test
     (ver riesgos). Claves a probar en 2.x: secret `type: s3`, `endpoint`,
     `use_ssl: false`, credenciales del bootstrap.
  3. dbt v2 (2.0.0, sep-2026) cambia el modelo a `catalogs.yml`; se recomienda
     quedarse en la línea 1.x para el PoC (estable y verificada aquí).
- Workaround vistas-sobre-Parquet: **no necesario** (plan B descartado).

## 1.3 Matriz de versiones a fijar

| Componente | Versión fijada | Origen / verificación |
|---|---|---|
| `dlt` (+extras `ducklake,s3,postgres,sql-database`) | **1.31.0** | PyPI + smoke ✅ |
| `duckdb` (python) | **1.5.6** | PyPI + smoke ✅ (DuckLake 1.0 exige DuckDB ≥1.5.2) |
| `dbt-core` | **1.12.5** | PyPI + smoke ✅ |
| `dbt-duckdb` | **1.11.0** | PyPI + smoke ✅ |
| `sqlalchemy` / `psycopg2-binary` | **2.1.4 / 2.9.13** | resueltos en el smoke ✅ |
| `prefect` (imagen `prefecthq/prefect:X-python3.12`) | **3.8.8** | PyPI + manifest ✅ (`prefect server start` desde la imagen) |
| Python base | **3.12** | venv local 3.12 ✅ (dlt admite 3.10–3.14) |
| `postgres` (catálogo + origen) | **18** (pinear digest en 2.1) | manifest `postgres:18` ✅, smoke con PG18 ✅ |
| `dxflrs/garage` | **v2.4.1** (pinear digest en 2.1) | manifests v2.3.0/v2.4.1 ✅; flags `--single-node --default-bucket` desde v2.3.0 |
| DuckLake spec / extensión | **1.0 / bundled** | DuckLake 1.0 (abr-2026) + DuckDB ≥1.5.2; `INSTALL ducklake` OK ✅ |

## Bloqueos y riesgos residuales

- **Ningún bloqueo**: ambos workarounds predefinidos quedan descartados.
- Riesgo residual (no bloqueante): pata S3 vía Garage sin smoke test todavía
  (dlt `storage.bucket_url=s3://` con `endpoint_url` custom + secret S3 de
  DuckDB con endpoint custom). Deuda explícita de los grupos 2–3 con la
  configuración exacta a probar ya identificada.

## Docs consultadas (verificadas 2026-10-08, no inventadas)

- https://dlthub.com/docs/dlt-ecosystem/destinations/ducklake (v1.31.0)
- https://dlthub.com/docs/dlt-ecosystem/verified-sources/sql_database/setup
- https://dlthub.com/docs/release-notes/1.17 (destino DuckLake desde 1.17)
- https://github.com/duckdb/dbt-duckdb/blob/master/README.md (attach DuckLake,
  `options` arbitrarios, `partitioned_by`/`sorted_by`, restricción MERGE)
- https://docs.getdbt.com/docs/local/connect-data-platform/duckdb-setup
  (actualizada 2026-09-16)
- https://ducklake.select/docs/stable/duckdb/usage/connecting (sintaxis ATTACH
  postgres + S3, parámetros METADATA_SCHEMA/DATA_PATH)
- https://duckdb.org/docs/current/core_extensions/ducklake (DuckLake 1.0,
  abr-2026)
- Versiones PyPI vía `pip index versions` + METADATA de wheels
  `dlt-1.31.0` y `dbt_duckdb-1.11.0` (evidencia en `/tmp/opencode/wheelmeta`).
