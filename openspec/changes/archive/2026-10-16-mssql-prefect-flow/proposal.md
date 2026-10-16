## Why

El flow Prefect `dulse-medallion` solo orquesta la fuente postgres; la ingesta MSSQL (el dato real) corre manual. Sin integrarla, el pipeline diario no es operable: alguien tiene que acordarse de restaurar e ingerir a mano cada día.

## What Changes

- El flow detecta si hay un `.bak` nuevo (marca de último restore) y, si lo hay, lo copia y restaura automáticamente; si no, salta el paso.
- Nueva rama de ingesta MSSQL con el path rápido (`fast_mssql.py`) en paralelo a la ingesta postgres, con `dbt run`/`dbt test` corriendo solo si ambas ramas tienen éxito.
- Todo parametrizado por env (paths del `.bak`, DB, tablas, ventana); mismos reintentos, logs y visibilidad en server que las tareas existentes.

## Capabilities

### New Capabilities

- `bak-watch-restore`: detección de `.bak` nuevo y restore condicional dentro del flow.
- `mssql-flow-branch`: rama de ingesta MSSQL (fast path) en paralelo a postgres con fail-stop conjunto.

### Modified Capabilities

- Ninguna (sin specs previas modificadas: `openspec/specs/orchestration/` no existe; el flow actual solo se extiende).

## Impact

- Solo `orchestration/` (flow + README + requirements si hace falta); `bronze/`, `dbt/`, compose y servicios intactos.
- El flow pasa a depender del path del `.bak` diario por env (`MSSQL_BAK_PATH`); sin esa variable, la rama MSSQL se salta con warning en vez de fallar.
- Riesgo: ramas en paralelo compiten por MSSQL/DuckDB; se mitiga con el grado de paralelismo ya calibrado y reintentos.
