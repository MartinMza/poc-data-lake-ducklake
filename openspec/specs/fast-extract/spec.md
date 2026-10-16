# fast-extract Specification

## Purpose
TBD - created by archiving change mssql-fast-ingest. Update Purpose after archive.
## Requirements
### Requirement: Export bcp paralelo a staging
El sistema SHALL proveer un script que exporte cada tabla con `bcp` nativo en paralelo (grado configurable) y la cargue con `COPY` de DuckDB al esquema `staging_mssql` del mismo lake, preservando tipos y sin pérdida.

#### Scenario: Export completo del subset
- **WHEN** se ejecuta el export sobre las 7 tablas del subset PoC
- **THEN** staging contiene las 7 tablas con conteos idénticos al origen y la fase es más rápida que el extract ODBC medido en la base

### Requirement: Benchmark de línea base previo
Antes de reemplazar nada, el sistema SHALL publicar tiempos por fase (restore, extract, merge) y por tabla del path actual, como referencia del speedup.

#### Scenario: Números publicados
- **WHEN** se lee el doc de benchmark del slice
- **THEN** constan segundos por fase y por tabla del path dlt actual sobre la DB real

