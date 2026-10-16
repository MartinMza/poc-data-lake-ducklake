## Context

El analytics actual vive en Snowflake con origen en Aurora. Este PoC replica esa arquitectura medallion (bronze / silver / gold) en un stack 100% open source y autohospedado para < 300 GB: DuckLake (Parquet en storage S3 + catálogo en Postgres) como formato/tabla, DuckDB como motor, dlt para ingesta, dbt-core + dbt-duckdb para transformaciones y Prefect para orquestación. Todo debe levantar con `docker compose` en local, con datos sintéticos y versiones fijadas.

Estado actual: repo greenfield sin specs previas. La mayor incertidumbre es la madurez del soporte DuckLake en `dlt` y `dbt-duckdb` (API reciente, puede haber cambiado), por lo que el diseño adopta una estrategia *verify-first*: verificar contra documentación actual antes de implementar y tener workarounds definidos.

Stakeholders: equipo que decidirá si avanzar con la migración (consume el documento de hallazgos y el benchmark).

## Goals / Non-Goals

**Goals:**
- PoC punta a punta reproducible: `docker compose up` + un bootstrap dejan el lakehouse operativo.
- Pipeline idempotente bronze → silver → gold con incrementalidad real y orquestación única.
- Evidencia objetiva: benchmark (ingesta + 3 queries, 10 GB / 50 GB), prueba de concurrencia, backup/restore y time travel.
- Decisión fundamentada de avanzar o no, incluyendo capa de servicio BI.

**Non-Goals:**
- Paridad total con Snowflake (sin RBAC fino, shares, ni multi-cluster).
- Alta disponibilidad o multi-nodo (Garage en nodo único es suficiente para el PoC).
- Datos reales, PII o conexiones a Aurora/Snowflake de producción.
- Optimización prematura de performance más allá de lo necesario para el benchmark.

## Decisions

### 1. Storage: Garage (nodo único) como S3 compatible
Data path de DuckLake (Parquet) sobre Garage en un solo nodo. Alternativas consideradas: SeaweedFS (mencionado como alternativa en el brief; más piezas móviles para el mismo objetivo) y MinIO (maduro pero sin ventaja decisiva para el PoC). Se elige Garage por simplicidad operativa en local y protocolo S3 estándar. Si Garage bloquea el PoC, el fallback es SeaweedFS con la misma interfaz S3, sin cambiar el resto del diseño.

### 2. Catálogo DuckLake en PostgreSQL dedicado
`postgres-catalog` aislado del origen simulado para que backup/restore y evolución del catálogo no se mezclen con los datos fuente. Alternativa (SQLite como catálogo) descartada: no representa el modo producción multi-proceso y complica la prueba de concurrencia.

### 3. Ingesta con dlt, Airbyte como alternativa descartada para el PoC
dlt es Python-nativo, liviano y encaja con el flow Prefect y con destinos custom si DuckLake no está soportado como destino nativo. Airbyte (ya en uso en producción) se descarta para el PoC por peso operativo (más servicios en el compose) salvo que dlt resulte inviable. Verificación previa obligatoria: si `dlt` no tiene destino DuckLake, workaround = destino `filesystem` (Parquet al bucket) + registro explícito en DuckLake, o staging vía Postgres; la decisión queda documentada en `bronze-ingestion`.

### 4. Transformaciones con dbt-core + dbt-duckdb
dbt aporta modelos versionados, tests (`not_null`, `unique`, `relationships`) y docs, que SQL suelto no da. Alternativa (scripts DuckDB puros) descartada porque perderíamos los tests exigidos por los criterios de éxito. Verificación previa: `profiles.yml` debe resolver el attach DuckLake (extensión/catalogo); si el adaptador no lo soporta, workaround = modelos contra Parquet + vistas de registro, documentado en `silver-gold-models`.

### 5. Orquestación con Prefect (server local en compose)
Prefect es más liviano que Airflow para un PoC local y modela bien el flow lineal ingesta → `dbt run` → `dbt test` con reintentos y logs. Alternativas (cron, Dagster, Airflow) descartadas por costo operativo o por no estar en el brief. El flow es el único entrypoint; cada etapa falla-cierra la cadena.

### 6. Layout medallion en un solo DuckLake, tres esquemas
`bronze` (réplica cruda + metadatos `_dlt`), `silver` (tipado/limpieza/dedup), `gold` (2–3 agregados solo desde silver). Un solo catálogo simplifica time travel, backup y el benchmark; separar en catálogos por capa se descarta por complejidad innecesaria a esta escala.

### 7. Capa de servicio opcional vía export a Postgres
Los BI consumen mejor Postgres que DuckDB embebido. Decisión diferida al documento de hallazgos: si el consumo directo (ej. lectura Parquet/DuckDB) es suficiente, no se construye; si no, job de export idempotente de gold → Postgres. No se asume de entrada.

## Risks / Trade-offs

- [Soporte DuckLake inmaduro en dlt/dbt-duckdb] → Mitigación: verificación documental previa con versiones y enlaces; workarounds predefinidos; hallazgo explícito si se usan.
- [Garage nodo único sin durabilidad real] → Mitigación: aceptado para el PoC; backup probado cubre la narrativa de recuperación; no se extrapola a producción sin cluster real.
- [Semántica de concurrencia DuckLake+DuckDB menos probada que Snowflake] → Mitigación: experimento dedicado 2 lectores + 1 escritor como criterio de éxito; si falla, queda como riesgo bloqueante en hallazgos.
- [Performance local (50 GB) limitada por disco/CPU del host] → Mitigación: benchmark documenta hardware y perfiles; compara 10 GB vs 50 GB en vez de prometer SLAs.
- [Deriva de versiones (API DuckLake cambiante)] → Mitigación: pinning total + matriz de compatibilidad con fecha de consulta.

## Migration Plan

No aplica migración productiva (PoC aislado, sin despliegue). Secuencia de puesta en marcha: `docker compose up` → bootstrap → verificación DuckLake → seed sintético → pipeline inicial → dbt → Prefect → benchmark/backup/time-travel. Rollback = `docker compose down -v` + restore desde backup probado; todo reproducible desde cero.

## Open Questions

- Versiones exactas a fijar (resolver en implementación contra docs actuales de DuckDB/DuckLake, dlt, dbt-duckdb, Prefect, Postgres, Garage).
- Definición de las 3 queries típicas del benchmark (proponer: agregación por cliente/mes, join pagos↔unidades con filtro temporal, scan con filtro de alta selectividad).
- Decisión gold-serving sí/no (resolver tras ver compatibilidad BI en el PoC).
- SeaweedFS como fallback: ¿activarlo solo si Garage bloquea, o dejarlo como nota?
