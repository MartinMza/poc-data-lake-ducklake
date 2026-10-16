## Why

Necesitamos validar si un lakehouse 100% open source y autohospedado (DuckLake + DuckDB) puede reemplazar el stack actual Snowflake + Aurora para un volumen < 300 GB. Sin esta validación no podemos decidir si avanzar con una migración que reduzca costo y dependencia cloud.

## What Changes

- Infraestructura local reproducible con `docker compose`: `postgres-catalog` (metadata DuckLake), `postgres-source` (origen simulado), Garage (S3 compatible, nodo único), `prefect-server`.
- Script de bootstrap idempotente: crea bucket, credenciales S3 y base del catálogo; verifica que DuckDB adjunta el DuckLake (catálogo Postgres + data path en Garage) y crea/lee una tabla.
- Esquema de ejemplo en `postgres-source` (3–5 tablas relacionadas, ej. clientes, pagos, unidades) con generador de datos sintéticos de volumen configurable.
- Pipeline de ingesta bronze con dlt (`postgres-source` → DuckLake) con carga incremental por columna de actualización.
- Proyecto dbt-core + dbt-duckdb con modelos silver (limpieza, tipado, dedup) y gold (2–3 agregados de negocio) más tests básicos (`not_null`, `unique`, `relationships`).
- Flow de Prefect que orquesta ingesta → `dbt run` → `dbt test`, con reintentos y logs.
- Capa de servicio opcional: exportar gold a Postgres para consumo BI (alcance a decidir según hallazgo de compatibilidad).
- Scripts de backup (dump del catálogo + sync del bucket) y restore, más prueba de time travel / snapshot.
- Benchmark documentado: tiempos de ingesta y 3 queries típicas con datos sintéticos escalados (10 GB y 50 GB).
- Documento de hallazgos: limitaciones, riesgos y recomendación de avanzar o no.
- Verificación previa obligatoria del soporte DuckLake en `dbt-duckdb` y `dlt` contra documentación actual; si algo no está soportado, se documenta y se propone workaround (no se asume compatibilidad).
- Versiones fijadas (pinned) de todas las imágenes y paquetes.

## Capabilities

### New Capabilities

- `lakehouse-infra`: compose, bootstrap (bucket, credenciales, catálogo) y verificación de attach DuckLake con DuckDB.
- `source-dataset`: esquema de origen simulado y generador sintético configurable.
- `bronze-ingestion`: pipeline dlt incremental hacia bronze en DuckLake.
- `silver-gold-models`: modelos dbt silver/gold y tests de datos.
- `orchestration`: flow Prefect ingesta → dbt run → dbt test.
- `operations`: backup/restore, time travel, benchmark, capa de servicio opcional y documento de hallazgos.

### Modified Capabilities

- Ninguna (repo sin specs previas; PoC greenfield, no cambia comportamiento existente).

## Impact

- Sistemas afectados: ninguno en producción; todo corre en local con `docker compose`, datos sintéticos, sin credenciales ni datos reales.
- Nuevos componentes en el repo: `docker-compose.yml`, scripts de bootstrap/backup/restore, generador sintético, pipeline dlt, proyecto dbt, flows Prefect, README y doc de hallazgos/benchmark.
- Dependencias a fijar: imágenes (`postgres`, `garage`, `prefect-server`) y paquetes (`duckdb`, `dlt`, `dbt-core`, `dbt-duckdb`, `prefect`); riesgo principal es soporte inmaduro de DuckLake en `dlt`/`dbt-duckdb`, mitigado con verificación documental previa y workarounds.
