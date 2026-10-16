## Why

Todos los días llega un `.bak` full de 7,5 GB que no se puede cambiar (sin diferenciales, sin CDC). El path actual (restore + extract ODBC fila a fila + merge dlt) es demasiado lento para una ventana diaria y escala mal con las 779 tablas. Hay que acelerar la ingesta actuando solo del lado del lake.

## What Changes

- Benchmark de línea base que instrumenta cada fase (restore, extract, merge) por tabla para saber dónde está el cuello.
- Path rápido de extracción: `bcp` nativo en paralelo por tabla → `COPY` de DuckDB a esquema `staging` → `MERGE` SQL hacia `bronze` (mismas tablas y columnas de metadatos, silver no se entera).
- Extracción por ventana temporal en tablas grandes inmutables (solo últimos N días por columna fecha) con resync full semanal que corrige modificaciones tardías.
- El pipeline dlt actual queda como fallback para tablas chicas; el path rápido lo reemplaza donde mida mejor.
- Criterio de éxito numérico: speedup >= 5x en el subset de 7 tablas con paridad total de conteos y sumas.

## Capabilities

### New Capabilities

- `fast-extract`: export `bcp` paralelo + carga `COPY` a staging.
- `windowed-merge`: MERGE staging → bronze con ventana temporal parametrizada y resync semanal.

### Modified Capabilities

- Ninguna (sin specs previas en `openspec/specs/`; el pipeline dlt existente no cambia comportamiento).

## Impact

- Nuevo código en `bronze/` (scripts de export/MERGE) y esquema `staging_mssql` en el mismo lake (transitorio por corrida).
- Sin cambios en el origen, el `.bak`, el compose ni los servicios; sin dependencias nuevas salvo `bcp` (ya viene en la imagen MSSQL).
- Riesgo principal: divergencia de esquemas bronze si el MERGE no replica las columnas de metadatos dlt; se mitiga con validación de paridad automatizada.
