# Ingesta bronze — SQL Server → DuckLake

La única fuente de datos del lago es SQL Server: el `.bak` real (`FAF12_Kavos`)
restaurado en el contenedor `mssql`. El origen Postgres sintético, sus tablas y
las capas dbt (`silver`/`gold`) fueron retirados.

Dos caminos de ingesta, mismo destino (esquema `bronze` del mismo DuckLake:
mismo catálogo Postgres y mismo `DATA_PATH` S3 vía Garage):

| Camino | Archivo | Rol |
|---|---|---|
| Fast path (bcp → staging → MERGE) | `fast_mssql.py` | **principal** (2026-10-16, speedup 5,4x con paridad total) |
| dlt (`sql_database` + pyodbc) | `pipeline_mssql.py` | fallback / rollback (re-correr converge por merge) |

## Versiones

- `dlt[ducklake,s3,sql-database]==1.31.0` (destino nativo `ducklake` desde 1.17.0)
- `duckdb==1.5.6` (DuckLake 1.0 exige ≥1.5.2)
- `sqlalchemy==2.1.4`, `pyodbc==5.3.0` + Microsoft ODBC Driver 18 en user-space
  (`~/.local/mssql-odbc`, sin root; `pipeline_mssql.py` precarga
  `libodbc.so.2`/`libodbcinst.so.2` y fija `ODBCSYSINI`).
- `bcp`/`sqlcmd` vienen de mssql-tools (host: `~/.local/mssql-odbc/bin`, contenedor: `/opt/mssql-tools18/bin`).

## Configuración (env, mismos defaults que compose/`.env.example`)

| Var | Default | Nota |
|---|---|---|
| `MSSQL_HOST` / `MSSQL_PORT` | `localhost` / `1433` | contenedor `mssql` |
| `MSSQL_DATABASE` | `FAF12_Kavos` | DB restaurada del `.bak` real |
| `MSSQL_USER` / `MSSQL_PASSWORD` | `sa` / `MSSQL_SA_PASSWORD` | credenciales del PoC local |
| `MSSQL_TABLES` | las 7 tablas reales (`DEFAULT_MSSQL_TABLES`) | `Tabla:pk:estrategia,...` |
| `MSSQL_PIPELINE_NAME` | `bronze_mssql_ingestion` | estado/cursor dlt separado |
| `CATALOG_*` | `ducklake:ducklake@localhost:5433/ducklake_catalog` | catálogo (`catalog` sin sufijo driver) |
| `GARAGE_S3_*`, `DUCKLAKE_DATA_PATH` | endpoint `http://localhost:3900`, `s3://lakehouse/data/` | storage del lake |
| `FAST_MSSQL_PARALLEL` | nCPUs | export bcp concurrente |
| `FAST_MSSQL_WINDOW_DAYS` | `15` | ventana incremental de `BSTransaccion` |
| `FAST_MSSQL_WORKDIR` | `mssql-fast/` | TSVs transitorios (gitignored) |

Estrategias: `merge` (upsert por PK, default cuando hay PK), `replace` (sin PK,
caso `DMart_*`), `incremental` (cursor `updated_at`; ninguna de las 7 reales lo
usa). El esquema real no tiene `rowversion` ni `updated_at` fiable, por eso el
default es full-extract + merge/replace.

## Uso

```bash
uv venv bronze/.venv --python 3.12
uv pip install -p bronze/.venv/bin/python -r bronze/requirements.txt

# Diario (fast path): BSTransaccion por ventana, resto full
bronze/.venv/bin/python bronze/fast_mssql.py all

# Carga completa y resync semanal
bronze/.venv/bin/python bronze/fast_mssql.py all --full
bronze/.venv/bin/python bronze/fast_mssql.py all --tables BSTransaccion --resync

# Paridad fast vs ground truth
bronze/.venv/bin/python bronze/fast_mssql.py parity --ref bronze/fast_ground_truth.json

# Fallback dlt (mismas tablas, converge por merge)
bronze/.venv/bin/python bronze/pipeline_mssql.py [--full-refresh] [--tables BSMoneda]
```

## Data cargada (subset real, 2026-10-15)

| Origen (`dbo`) | Filas | PK | Estrategia | Bronze |
|---|---|---|---|---|
| `BSMoneda` | 17 | `MonedaID` | merge | `mssql_bs_moneda` |
| `BSProducto` | 9 342 | `ProductoID` | merge | `mssql_bs_producto` |
| `BSOrganizacion` | 3 055 | `OrganizacionID` | merge | `mssql_bs_organizacion` |
| `BSTransaccion` | 320 447 | `TransaccionID` | merge | `mssql_bs_transaccion` |
| `BSOperacion` | 148 356 | `TransaccionID` | merge | `mssql_bs_operacion` |
| `DMart_Ventas` | 275 | — | replace | `mssql_d_mart_ventas` |
| `DMart_VentasProductoCliente` | 4 065 | — | replace | `mssql_d_mart_ventas_producto_cliente` |

Total 485 557 filas, convergen sin duplicados en re-corridas. Paridad fast vs
dlt verificada (`bronze/fast_ground_truth.json`: conteos y sumas al centavo).

## Mapeo de tipos observado (sin pérdida)

`MONEY→DECIMAL(38,9)` (sumas exactas), `DATETIME/DATETIME2→TIMESTAMP` (ms
preservados), `BIT→BOOLEAN`, `VARCHAR/NVARCHAR→VARCHAR` (unicode intacto),
`INT/BIGINT/SMALLINT`, `NUMERIC`, `DATE`, `TEXT→VARCHAR`. Tipos fuera del
mapeo (`IMAGE`, `XML`, `GEOGRAPHY`) abortan en `check_source_types` hasta
ampliarlo caso por caso.

**Nombres de columna**: el fast path normaliza a `snake_case` (`transaccion_id`,
`importe_total`); el camino dlt conserva el nombre original del origen. La API
y el MCP exponen lo que haya en el lago (hoy `snake_case`).

## Gotchas

1. `--full-refresh` / `replace` reescriben la tabla: hay una ventana breve sin
   datos visible para consumidores. Para servir, usar snapshots/backup.
2. Las tablas de prueba (`mssql_productos`, `mssql_ventas`, `mssql_movimientos`)
   y la DB `RestoredProof` fueron eliminadas con `scripts/purge_sample_data.py`
   y `DROP DATABASE`. No hay default que apunte a ellas.
3. `TrustServerCertificate=yes` es solo-PoC-local (cert autofirmado).
4. No hay `requirements` para bcp: si falta `bcp`, el fast path falla y
   corresponde el fallback dlt (`FAST_MSSQL_MODE=dlt`).
