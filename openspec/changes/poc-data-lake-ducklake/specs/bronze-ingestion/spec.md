## ADDED Requirements

### Requirement: Compatibilidad dlt + DuckLake verificada
El proyecto SHALL documentar, contra la documentación actual de `dlt`, si existe destino DuckLake soportado; si no existe, SHALL documentar el workaround elegido (ej. destino filesystem Parquet + registro manual, o staging Postgres) antes de implementar el pipeline.

#### Scenario: Matriz de compatibilidad
- **WHEN** se revisa el doc de hallazgos/README de ingesta
- **THEN** consta la versión de `dlt` evaluada, el resultado (soportado/no soportado) con enlace a la doc y, en caso negativo, el workaround adoptado

### Requirement: Pipeline bronze incremental
El sistema SHALL proveer un pipeline `dlt` de `postgres-source` → esquema `bronze` en DuckLake con carga incremental por la columna de actualización, creando una tabla bronze por tabla origen con metadatos de ingesta (`_dlt_load_id` / `loaded_at` o equivalentes).

#### Scenario: Carga inicial y segunda corrida incremental
- **WHEN** se ejecuta el pipeline dos veces con cambios intermedios en el origen
- **THEN** la primera carga trae el volumen completo y la segunda solo trae las filas con `updated_at` posterior al cursor, sin duplicar las ya cargadas

### Requirement: Idempotencia y re-ejecución segura
El pipeline SHALL ser idempotente: re-ejecutar la misma carga (mismo cursor, mismos datos) no duplica filas en bronze ni corrompe el estado del cursor.

#### Scenario: Reintento tras fallo
- **WHEN** una corrida falla a mitad y se re-ejecuta con el mismo cursor
- **THEN** bronze converge al mismo estado que una corrida exitosa única y el cursor avanza solo al completarse
