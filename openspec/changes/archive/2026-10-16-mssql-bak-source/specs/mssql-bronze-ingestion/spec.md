## ADDED Requirements

### Requirement: Pipeline MSSQL → bronze en DuckLake
El sistema SHALL proveer un pipeline dlt que lea las tablas restauradas en MSSQL y las cargue en el esquema `bronze` del DuckLake existente (mismo catálogo y `DATA_PATH` que el pipeline de postgres), con metadatos de ingesta por fila y sin modificar el pipeline de postgres.

#### Scenario: Carga inicial desde MSSQL restaurado
- **WHEN** se ejecuta el pipeline con la base restaurada disponible
- **THEN** cada tabla origen tiene su tabla en `bronze` con el mismo conteo de filas y metadatos de ingesta

### Requirement: Estrategia incremental documentada
El pipeline SHALL implementar carga incremental (por columna de cursor si las tablas la tienen; si no, full-refresh diario con `merge` por PK sin duplicados) y la estrategia elegida por tabla SHALL quedar documentada en su README.

#### Scenario: Segundo backup diario sin duplicados
- **WHEN** se restaura el `.bak` del día siguiente y se ejecuta el pipeline
- **THEN** `bronze` refleja los datos nuevos sin duplicar filas ya cargadas

### Requirement: Tipos MSSQL mapeados sin pérdida
El pipeline SHALL mapear los tipos MSSQL relevantes (`DATETIME2`, `DECIMAL(p,s)`, `BIT`, `NVARCHAR`, `VARBINARY` documentando límite) a tipos DuckDB sin pérdida de precisión, y SHALL fallar con mensaje claro ante un tipo no soportado en lugar de truncar en silencio.

#### Scenario: Tipos críticos preservados
- **WHEN** una tabla contiene `DECIMAL(18,2)` y `DATETIME2`
- **THEN** los valores en `bronze` conservan escala y precisión idénticas al origen
