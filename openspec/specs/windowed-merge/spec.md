# windowed-merge Specification

## Purpose
TBD - created by archiving change mssql-fast-ingest. Update Purpose after archive.
## Requirements
### Requirement: MERGE staging → bronze con paridad
El sistema SHALL aplicar `MERGE` SQL de staging a las tablas `bronze` existentes (mismas columnas, incluidas `_dlt_load_id`/`_dlt_id` u equivalentes) de modo que silver y el pipeline legacy vean datos indistinguibles.

#### Scenario: Paridad con el path legacy
- **WHEN** se corre el path rápido y se comparan conteos y sumas de control contra una corrida dlt
- **THEN** son idénticos por tabla y los tests de dbt siguen en verde

### Requirement: Ventana temporal con resync semanal
Cada tabla grande configurable SHALL extraerse por defecto con filtro de ventana (`Fecha >= hoy - N días`, N por env) y SHALL existir un modo resync full que reprocese la tabla completa para corregir modificaciones tardías.

#### Scenario: Ventana + resync convergen
- **WHEN** se corre diario por ventana una semana y luego un resync full
- **THEN** bronze tras el resync es idéntico a un full diario de esa semana (sin filas perdidas ni duplicadas)

