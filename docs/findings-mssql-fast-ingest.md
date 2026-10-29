# Hallazgos — ingesta rápida MSSQL (`mssql-fast-ingest`)

> **Nota de vigencia (2026-10-26)**: este documento registra los hallazgos
> históricos del PoC. Después de esas mediciones se retiró el
> origen Postgres sintético y las capas dbt (`silver`/`gold`), y se agregó
> autenticación a la API/MCP. El lago hoy contiene solo las 7 tablas reales
> ingestadas desde SQL Server. Lo que sigue se conserva como evidencia de las
> corridas originales.

Fecha: 2026-10-16. DB real `FAF12_Kavos` ONLINE, subset 7 tablas (485 557 filas).
Script: `bronze/fast_mssql.py` (`export` bcp paralelo → `load` COPY a
`staging_mssql` → `merge` a `bronze` → `parity`/`snapshot`). Detalle operativo
en `bronze/README.md` (§ path rápido). No se tocó `pipeline_mssql.py`
(solo import read-only), `pipeline.py`, `dbt/`, `orchestration/`, compose.

## 1. Línea base del path dlt (tarea 1.1, DB real, sin contention)

| Fase | Medición |
|---|---|
| restore 7,5 GB → `FAF12_Bench` (test, luego dropeada) | **132 s** (one-time; en producción corre a diario antes del extract) |
| fetch ODBC puro (pyodbc, sin procesar) | 10,9 s (BSTransaccion 4,3 + BSOperacion 6,3 + resto 2,3) |
| dlt 7 tablas, 1 corrida (`bronze_mssql_baseline2`) | **65,0 s** |
| dlt por tabla, suma (`bronze_mssql_baseline3`, limpio) | 68,8 s (1,4 / 2,3 / 1,3 / **29,7** / **32,3** / 0,8 / 1,0) |

Lectura: el cuello NO es el extract (10,9 s) sino normalize+merge Python de
dlt (~54 s, 83 %). Confirma la decisión del design (bcp + MERGE SQL).

## 2. Path rápido: números finales (tarea 2.x + 4.1)

`all --full`, 7 tablas en cadenas export→load→merge paralelas: **12 s wall**
(export ≈7 + load ≈3 + merge ≈2 solapados; tabla lenta = BSTransaccion).

| Tabla | Filas | dlt (s) | fast full (s) | Speedup | Decisión |
|---|---|---|---|---|---|
| BSMoneda | 17 | 1,4 | 0,8 | 1,8x | **bcp principal** |
| BSProducto | 9 342 | 2,3 | 1,4 | 1,6x | **bcp principal** |
| BSOrganizacion | 3 055 | 1,3 | 1,0 | 1,3x | **bcp principal** |
| BSTransaccion | 320 447 | 29,7 | 8,5 (3,2 ventana) | 3,5x (9,3x ventana) | **bcp + ventana 15 d principal** |
| BSOperacion | 148 356 | 32,3 | 5,0 | 6,5x | **bcp principal (full siempre)** |
| DMart_Ventas | 275 | 0,8 | 0,8 | 1,0x | **bcp principal** (un solo path operativo) |
| DMart_VentasProductoCliente | 4 065 | 1,0 | 0,9 | 1,1x | **bcp principal** (idem) |
| **TOTAL** | **485 557** | **65,0** | **12** | **5,4x** | **target 5x CUMPLIDO** |

Paridad: **TOTAL** en las 7 (1 017 checks: conteos + nulos por columna +
sumas DECIMAL(38,9) + `''` por string) contra snapshot legacy dlt, más
`dbt test` **28/28 PASS**. Referencia versionada: `bronze/fast_ground_truth.json`
(`snapshot`/`parity` re-ejecutables).

## 3. Decisiones validadas con datos (2.1–2.2)

- **Tipos**: `MONEY`/`DATETIME` crudos de bcp parsean exacto en DuckDB
  (`100000.0000`, `2013-08-14 00:00:00.000`); solo se convierten a ISO-126
  por canonicidad (0,4 s). `BIT` 0/1 → BOOLEAN directo. Eñes/tildes/€ intactos
  (`-C 65001`; `Dólares`, `€` verificados byte a byte).
- **`''` vs `' '` vs NULL**: T-SQL `' ' = ''` es TRUE → el sentinel de vacíos
  usa `DATALENGTH(...) = 0` (148 194 `CodOperacion=' '` preservados como dlt).
- **`\t\n\r` (212 filas)**: REPLACE solo en columnas sucias (scan de 1
  full-scan por tabla); resto CAST directo. El REPLACE ciego costaba 7,5 s.
- **Delimiter/quote**: `QUOTE=CHAR(31)` (comillas dobles literales, ~1 700
  celdas), `HEADER false` obligatorio (DuckDB salta la 1ª fila de los `.tsv`
  por autodetección — bug real encontrado por la aserción líneas-vs-filas).
- **Staging en orden origen** con tipos bronze (dlt reordena MONEY al final;
  COPY es posicional — segundo bug real, mismo hallazgo).
- **NUL quirk**: bcp crudo emite `0x00` en TEXT NULL → strings siempre con
  `CAST(... AS nvarchar(max))`, nunca crudos.

## 4. Ventana temporal (tarea 3.1)

- Solo `BSTransaccion.FechaUltimaModificacion` califica (única fecha de
  actividad; `OR ... IS NULL` incluye sus 48 946 NULLs: correcto, 50 376 filas
  ventana vs 320 447 full). `BSOperacion` **siempre full** (fechas de
  vencimiento/viaje, no de actividad — por design van full las sin fecha
  fiable); tablas chicas siempre full (overhead fijo ~0,7 s domina).
- Validación: 3 ventanas con cutoffs distintos + `--resync` → **PARIDAD TOTAL**
  (sin filas perdidas ni duplicadas). Ventana diaria BSTransaccion: 3,2 s.
- Env: `FAST_MSSQL_WINDOW_DAYS` (15), `FAST_MSSQL_WINDOW_<TABLA>`,
  `--cutoff YYYY-MM-DD` (snapshots), `--resync`, `FAST_MSSQL_PARALLEL` (=nCPUs).

## 5. Qué limita (por qué no mucho más de 5x)

1. bcp server-side evalúa expresiones por fila (~2–3 s en las grandes).
2. Overhead fijo por tabla (~0,7 s: spawns `docker compose exec`+`cp`,
   connects ODBC, 3 attaches DuckLake por cadena).
3. El catálogo DuckLake serializa MERGEs concurrentes (merge paralelo ≈
   secuencial). Chicas ~1x: ganan igual por path único, no por velocidad.

## 6. Rollback y fallback

`pipeline_mssql.py` intacto y verificado (baselines 1.1 corrieron con él):
rollback = re-correrlo (merge converge). dlt queda como fallback declarado
para tablas chicas/futuras donde bcp no mida mejor.
