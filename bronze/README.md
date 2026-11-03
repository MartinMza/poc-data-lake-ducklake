# Carga Bronze — Parquet → DuckLake

La única fuente de datos del lago es SQL Server vía backup `.bak`. El camino
de datos tiene dos pasos y un solo destino (esquema `bronze` del mismo
DuckLake: mismo catálogo Postgres y mismo `DATA_PATH` S3 vía Garage):

1. **Extracción** (`bak2parquet/`, paquete aparte): restaura el `.bak` en una
   base temporal, vuelca cada tabla a `<out>/<esquema>/<tabla>.parquet`
   (compresión ZSTD) y elimina la base al terminar.
2. **Carga** (`bronze/load_parquet.py`, este directorio): lee ese árbol con
   `read_parquet` de DuckDB y reemplaza cada tabla entera en `bronze`
   (idempotente: cada backup se extrae completo, así que no hay allowlist ni
   modos por tabla).

## Versiones

- `duckdb==1.5.6` (DuckLake 1.0 exige DuckDB ≥1.5.2)
- `pyarrow==21.0.0` (versión ya usada en el repo, paquete del extractor)

## Configuración (env, mismos defaults que compose/`.env.example`)

El cargador solo necesita el lago (el origen lo resolvió el extractor):

| Var | Default | Nota |
|---|---|---|
| `CATALOG_*` | `ducklake:ducklake@localhost:5433/ducklake_catalog` | catálogo (`catalog` sin sufijo driver) |
| `GARAGE_S3_*`, `DUCKLAKE_DATA_PATH` | endpoint `http://localhost:3900`, `s3://lakehouse/data/` | storage del lake |

Conexión vía `scripts/lake_counts.connect()` (SECRET S3 + ATTACH canónicos,
mismo `DATA_PATH` que el resto del repo).

## Uso

```bash
uv venv bronze/.venv --python 3.12
uv pip install -p bronze/.venv/bin/python -r bronze/requirements.txt

# 1. Extraer el .bak a Parquet (restaura temporal en el motor existente y limpia)
PYTHONPATH=bak2parquet/src bronze/.venv/bin/python -m bak2parquet --engine url \
  --bak mssql-backup/<dump-del-dia>.bak --out parquet-out

# 2. Cargar el Parquet en Bronze (reemplazo total por tabla)
bronze/.venv/bin/python bronze/load_parquet.py --src parquet-out
bronze/.venv/bin/python bronze/load_parquet.py --src parquet-out --report-json parquet-out/bronze-load-report.json
```

Salida por tabla (`load bronze.mssql_bs_moneda: 17 filas (0.4s)`) y un total
final. Con `--report-json` además escribe el resumen (filas, segundos y
origen por tabla). Exit 0 solo si todas las tablas cargaron; si alguna falla,
la lista en stderr y sale 1 (re-correr converge: lo ya cargado queda igual).

## Nombres y tipos

- Destino: `bronze.mssql_<tabla>` en `snake_case` (`dbo.BSTransaccion` ->
  `mssql_bs_transaccion`; esquemas no-`dbo` agregan su esquema). Mismo contrato
  de nombres que la ingesta anterior: la API y el MCP exponen `mssql_*`.
- Columnas: se normalizan a `snake_case` (`TransaccionID` ->
  `transaccion_id`). Si dos columnas colisionan al normalizar, la tabla se
  aborta con el detalle en vez de pisarse en silencio.
- Tipos: los que trae el Parquet del extractor (numéricos exactos, fechas con
  precisión, booleanos, textos, nulos). Tablas vacías generan Parquet válido
  con esquema y cargan 0 filas.

## Data cargada (subset real, 2026-10-15)

| Origen (`dbo`) | Filas | Bronze |
|---|---|---|
| `BSMoneda` | 17 | `mssql_bs_moneda` |
| `BSProducto` | 9 342 | `mssql_bs_producto` |
| `BSOrganizacion` | 3 055 | `mssql_bs_organizacion` |
| `BSTransaccion` | 320 447 | `mssql_bs_transaccion` |
| `BSOperacion` | 148 356 | `mssql_bs_operacion` |
| `DMart_Ventas` | 275 | `mssql_d_mart_ventas` |
| `DMart_VentasProductoCliente` | 4 065 | `mssql_d_mart_ventas_producto_cliente` |

Total 485 557 filas, convergen sin duplicados en re-corridas.

## Gotchas

1. El reemplazo total reescribe la tabla: hay una ventana breve sin datos
   visible para consumidores. Para servir, usar snapshots/backup.
2. Las tablas de prueba (`mssql_productos`, `mssql_ventas`, `mssql_movimientos`)
   y la DB `RestoredProof` fueron eliminadas con `scripts/purge_sample_data.py`
   y `DROP DATABASE`. No hay default que apunte a ellas.
3. Si la extracción falla (backup corrupto o versión incompatible), el
   extractor aborta con el motivo del motor en stderr y el cargador no
   corre: ese backup no deja Bronze a medio escribir.
