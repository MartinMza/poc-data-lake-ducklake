## Why

El operador recibe un backup diario en formato `.bak` de SQL Server y hoy no tiene forma de cargarlo ni de llevar esos datos al datalake. Sin este change, esa fuente queda fuera del PoC y no se puede validar el caso de uso real de ingesta.

## What Changes

- Nuevo servicio `mssql` en `docker-compose.yml` (imagen fijada, password por env, volumen para alojar el `.bak`), sin tocar los servicios existentes.
- Script de restore idempotente que carga el `.bak` diario en la instancia MSSQL (con `MOVE` de archivos y verificación de tablas).
- Pipeline de ingesta (dlt) desde MSSQL restaurado hacia el esquema `bronze` del DuckLake existente, reutilizando el patrón incremental del pipeline de postgres.
- El `.bak` lo aporta el operador (no se commitea al repo); se documenta dónde colocarlo y qué versión de SQL Server lo generó.

## Capabilities

### New Capabilities

- `mssql-service`: contenedor SQL Server en el compose con imagen fijada, credenciales por env y volumen para el `.bak`.
- `bak-restore`: restore idempotente del backup diario en la instancia MSSQL con verificación post-restore.
- `mssql-bronze-ingestion`: pipeline dlt MSSQL → `bronze` en DuckLake con estrategia incremental documentada.

### Modified Capabilities

- Ninguna (sin specs previas en `openspec/specs/`; el pipeline de postgres existente no cambia comportamiento).

## Impact

- `docker-compose.yml`: un servicio nuevo (`mssql`); resto intacto.
- Nuevo código: script(s) de restore y pipeline `bronze/` adicional (o extensión del existente) más dependencia de driver MSSQL en el venv de bronze.
- El `.bak` y las credenciales SA quedan fuera del repo (volumen local + `.env` ignorado).
- Riesgo principal: compatibilidad de versión (la instancia debe ser >= la versión que generó el `.bak`) y estrategia incremental si las tablas no tienen columna de cursor.
