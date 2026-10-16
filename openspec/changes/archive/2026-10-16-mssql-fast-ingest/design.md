## Context

El `.bak` full diario (7,5 GB, 779 tablas, SQL 2019) no es negociable: sin diferenciales ni CDC. El path actual dlt/pyodbc funciona pero es lento porque extrae fila a fila y mezcla por PK en Python. El lake ya tiene el subset de 7 tablas en bronze con sumas de control conocidas, que sirve como ground truth de paridad.

## Goals / Non-Goals

**Goals:**
- Speedup >= 5x en el subset PoC con paridad total (conteos + sumas + dbt tests verdes).
- Ventana diaria viable aunque el subset crezca a decenas de tablas.

**Non-Goals:**
- Cambiar nada del lado origen (backup, esquema, CDC).
- Reemplazar el pipeline dlt donde ya es suficiente (tablas chicas); queda como fallback.
- Tocar silver/gold: el contrato bronze no cambia.

## Decisions

### 1. `bcp out` en paralelo + `COPY` en vez de ODBC
`bcp` es la vía nativa más rápida de SQL Server y ya está en el contenedor. Paralelismo por tabla con grado configurable (default = nº CPUs). Alternativa (mantener pyodbc con `fast_executemany`/arrays) descartada: sigue atada al round-trip por fila. `sqlcmd` + CSV descartado: más lento y con peor manejo de tipos que `bcp` nativo.

### 2. Staging en el mismo lake + `MERGE` SQL
Esquema `staging_mssql` transitorio (se trunca por corrida; opcional conservar última para auditoría). El `MERGE` corre en DuckDB, órdenes de magnitud más barato que el upsert en Python. Alternativa (reemplazar bronze con `COPY` directo) descartada: perdería historial y metadatos dlt que silver podría usar.

### 3. Ventana temporal solo en tablas grandes con fecha fiable
Filtro `WHERE fecha >= hoy - N` con N por tabla/env. Tablas sin fecha fiable van siempre full. Resync semanal full corrige lo tardío. Alternativa (hash-diff fila a fila) descartada por costo: comparar 5M+ filas por hash cada día es casi tan caro como el full.

## Risks / Trade-offs

- [Tipos `bcp` nativo vs ODBC: `MONEY`, `DATETIME`, collations pueden variar en formato] → Mitigación: validación de paridad automatizada (conteos + sumas + muestreo de filas) antes de declarar el path como principal.
- [Ventana pierde modificaciones tardías fuera de N días] → Mitigación: resync semanal + N conservador (15 días) en tablas de auditoría.
- [`bcp` paralelo satura MSSQL o disco] → Mitigación: grado configurable, default CPUs, y benchmark que lo calibra.

## Migration Plan

Sin migración: el path rápido convive con dlt. Orden: benchmark base → export/COPY → MERGE + paridad → ventana + resync → documentar y declarar path principal por tabla. Rollback = volver a `pipeline_mssql.py` (no se borra).

## Open Questions

- ¿Cuánto tarda hoy cada fase? (Lo mide la tarea 1.1; define el target absoluto.)
- ¿Qué tablas aceptan ventana (fecha fiable e inmutabilidad del pasado)? (Se decide con datos: 15% NULL en `FechaUltimaModificacion` ya descarta esa columna como cursor.)
- Grado de paralelismo óptimo en este host (se calibra en el benchmark).
