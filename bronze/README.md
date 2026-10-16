# Ingesta bronze (dlt → DuckLake)

Slice `bronze-ingestion` del change `poc-data-lake-ducklake` (tasks 4.1–4.3).
Pipeline `postgres-source` → esquema `bronze` en el **mismo** DuckLake del smoke
de infra (mismo catálogo postgres + mismo `DATA_PATH` S3 vía Garage).

## Archivos

| Archivo | Qué es |
|---|---|
| `pipeline.py` | Pipeline dlt: 4 recursos `sql_table` con incremental por `updated_at` + merge/upsert por PK |
| `requirements.txt` | Deps fijadas del venv propio (`dlt==1.31.0`, `duckdb==1.5.6`, …) |
| `.venv/` | Venv local (no commitear; crear con `uv venv bronze/.venv`) |

No se pisa `dbt/`, `orchestration/`, `docker-compose.yml`, `scripts/`, `source/`.

## Versiones (matriz compat 1.3)

- `dlt[ducklake,s3,postgres,sql-database]==1.31.0` (destino nativo `ducklake` desde 1.17.0)
- `duckdb==1.5.6` (DuckLake 1.0 exige ≥1.5.2; verificado con dlt)
- `sqlalchemy==2.1.4`, `psycopg2-binary==2.9.13`, `psycopg[binary]==3.2.9` (solo seed)
- NOTA: `scripts/` usa `duckdb==1.4.4` LTS en su propio venv → **bloqueo §5**
  (este venv es independiente y usa 1.5.6 verificado para dlt).

## Configuración (env, mismos defaults que compose/`.env.example`)

| Var | Default | Nota |
|---|---|---|
| `CATALOG_*` | `ducklake:ducklake@localhost:5433/ducklake_catalog` | `catalog` **sin** sufijo driver (`+psycopg2` falla) |
| `SOURCE_*` | `source_user:source_password@localhost:5434/source_db` | origen **sí** lleva `+psycopg2` (sqlalchemy) |
| `GARAGE_S3_*` | key `GK0123…`, endpoint `http://localhost:3900`, region `garage` | secret S3 con `URL_STYLE path`, `USE_SSL false` |
| `DUCKLAKE_DATA_PATH` | `s3://lakehouse/data/` | canónico del smoke; con `/` final |
| `DUCKLAKE_NAME` / `DUCKLAKE_METADATA_SCHEMA` | `lake` / `public` | `public` = reutiliza el lake del smoke (docs dlt "Metadata schema") |
| `PIPELINE_NAME` | `bronze_ingestion` | estado/cursor por pipeline (dlt state sync) |

`pipeline.py` lee `.env` si existe (igual que `scripts/smoke_ducklake.py`).

Gotchas aplicados (compat-matrix §1.1): `storage` anidado
`{"bucket_url": ..., "credentials": {...}}` (string plano se ignora),
`initial_value=datetime(..., tzinfo=utc)` (no string),
`automatic_migration=True` (ver §5).

## Uso

```bash
uv venv bronze/.venv --python 3.12
uv pip install -p bronze/.venv/bin/python -r bronze/requirements.txt

# 1. DDL + seed demo (100/50/200/~1200; ver §5 por bug OVERRIDING)
bronze/.venv/bin/python -c "import psycopg; ..."  # o psql -f source/ddl.sql
SOURCE_DSN=postgresql://source_user:source_password@localhost:5434/source_db \
  bronze/.venv/bin/python source/generate.py --mode seed --scale demo --out postgres
# workaround actual usado (sin tocar source/): INSERT ... OVERRIDING SYSTEM VALUE
# + setval() de secuencias (ver reporte del slice).

# 2. Carga inicial
bronze/.venv/bin/python bronze/pipeline.py

# 3. Tanda de updates (mueve updated_at) + incremental
SOURCE_DSN=... bronze/.venv/bin/python source/generate.py --mode update --out postgres
bronze/.venv/bin/python bronze/pipeline.py

# 4. Reintento idempotente (sin cambios)
bronze/.venv/bin/python bronze/pipeline.py

# Subset / full-refresh
bronze/.venv/bin/python bronze/pipeline.py --tables customers,payments
bronze/.venv/bin/python bronze/pipeline.py --full-refresh
```

Cada corrida imprime `bronze.<tabla>: N filas` + ejemplo `_dlt_load_id`
(metadatos de ingesta; dlt aporta `_dlt_load_id`/`_dlt_id` por fila).

## Validación 4.2–4.3 (2026-10-08, perfil demo, mismo lake S3)

Fuente tras seed: `customers 100, units 50, leases 200, payments 1190`.

| Corrida | dlt | bronze tras corrida | Lectura |
|---|---|---|---|
| 1 inicial | `1 load package LOADED` | `100 / 50 / 200 / 1190` (total 1540) | carga completa OK |
| 2 sin cambios | `load step did not start, 0 packages` | `100 / 50 / 200 / 1190` (igual) | incremental: 0 filas, sin duplicados |
| update fuente | `leases 10, payments 59 tocados, 119 nuevos` | fuente `... / 200 / 1309` | `generate.py --mode update` OK |
| 3 post-update | `1 package LOADED` | `100 / 50 / 200 / 1309` (total 1659 = 1540+119) | solo delta; `leases` sigue 200 (upsert), `payments` = fuente (no 1190+178) |
| 4 reintento | `1 package` solo `_dlt_pipeline_state`, `items_count=0` en las 4 tablas | `100 / 50 / 200 / 1309` (igual) | idempotente: 0 filas de datos, sin duplicados |

Chequeos extra: 0 grupos duplicados por PK en las 4 tablas;
`bronze.leases closed=57` = fuente `57`; `lease_id=197` idéntico en ambas;
`_dlt_load_id` con 2 valores distintos en `leases`/`payments` (carga 1 + delta).
Cursor dlt (`last_value` = max `updated_at`) avanza solo al éxito (state commit
de dlt; la corrida 4 reutiliza el mismo cursor y trae 0).

## Smoke S3-vía-Garage (deuda 1.3, saldada aquí)

`DATA_PATH s3://lakehouse/data/` con `endpoint_url http://localhost:3900`
funciona de punta a punta con dlt + duckdb 1.5.6: `bronze.*` legible desde
conexión DuckDB independiente y ficheros en `s3://lakehouse/data/bronze/`
(bucket `lakehouse`: 54 objetos, ~584 kB tras la validación; era 2 obj/644 B
antes). Equivalente al attach canónico del smoke
(`ATTACH 'ducklake:postgres:...' (DATA_PATH 's3://lakehouse/data/')`).

## §5 Bloqueos y deuda

1. **`source/generate.py --mode seed --out postgres` falla siempre** (no tocado
   por este slice): `INSERT` con IDs explícitos sobre columnas
   `GENERATED ALWAYS AS IDENTITY` → `psycopg.errors.GeneratedAlways`.
   Workaround usado (sin modificar `source/`): seed propio con
   `INSERT ... OVERRIDING SYSTEM VALUE` + `setval()` de las 4 secuencias
   (si no, los `INSERT` sin ID del `--mode update` colisionan en `1`).
   Fix propuesto (workstream 3): añadir `OVERRIDING SYSTEM VALUE` en
   `write_postgres()` o pasar a `GENERATED BY DEFAULT`.
2. **Migración del catálogo 0.3 → 1.0 rompe `scripts/smoke_ducklake.py`**
   (`duckdb==1.4.4` solo lee DuckLake ≤0.3; este pipeline con duckdb 1.5.6
   migra a 1.0 con `automatic_migration=True` y el smoke responde
   `NotImplementedException: Only ... 0.3 are supported`).
   El lake sigue sano en 1.0 (verificado con duckdb 1.5.6 + S3) y es el camino
   que piden compat-matrix (DuckLake 1.0) y dbt (`verifydbt` ya usa 1.5.6).
   Fix propuesto (infra): subir `scripts/requirements-smoke.txt` a
   `duckdb==1.5.6` (pin común con este venv).

---

# Ingesta bronze MSSQL (dlt → DuckLake) — slice `mssql-bak-source` 4.1–4.2

Pipeline `bronze/pipeline_mssql.py`: MSSQL restaurado → MISMO esquema `bronze`
del mismo DuckLake (mismo catálogo + `DATA_PATH`), tablas con prefijo `mssql_`.
No toca `pipeline.py`, `dbt/`, `orchestration/`, `docker-compose.yml`.
Validado contra DB de prueba `RestoredProof`; el `.bak` REAL aún no llega.

## Driver (decisión 3: pyodbc + ODBC 18, sin fallback)

- `pyodbc==5.3.0` (añadido a `requirements.txt`; `pipeline.py` no lo usa).
- Microsoft ODBC Driver 18 (`18.7.1.1`) + unixODBC (`libodbc2 2.3.12`)
  instalados en **user-space** (`~/.local/mssql-odbc`, sin root: `.deb`
  oficiales extraídos con `dpkg-deb -x`). `pymssql` NO fue necesario.
- `pipeline_mssql.py` se auto-configura: precarga `libodbc.so.2` /
  `libodbcinst.so.2` por ruta absoluta (el loader solo lee
  `LD_LIBRARY_PATH` al arranque) y fija `ODBCSYSINI` al `odbcinst.ini`
  user-space. Sin exports manuales; con `LD_LIBRARY_PATH`/`ODBCSYSINI`
  ya seteados (instalación con root) se respetan.
- `TrustServerCertificate=yes` solo-PoC-local (cert autofirmado del contenedor).

## Env (qué cambiar cuando llegue el `.bak` REAL)

| Var | Default (prueba) | Al llegar el real |
|---|---|---|
| `MSSQL_HOST` / `MSSQL_PORT` | `localhost` / `1433` (`MSSQL_PORT` del `.env`) | solo si cambia host/puerto |
| `MSSQL_DATABASE` | `RestoredProof` | **cambiar al nombre de la DB restaurada del `.bak` real** |
| `MSSQL_USER` | `sa` | idem |
| `MSSQL_PASSWORD` (fallback `MSSQL_SA_PASSWORD`) | `.env` | idem |
| `MSSQL_PIPELINE_NAME` | `bronze_mssql_ingestion` | estado/cursor separados del pipeline postgres |

`TABLES` en el script: `productos(sku)`, `ventas(id)`, `movimientos(mov_id)`
(PKs verificadas en `RestoredProof`); ajustar si el esquema real difiere.

## Estrategia por tabla: incremental por `updated_at` + merge por PK

Las 3 tablas de prueba tienen `updated_at` (DATETIME2, cursor naive
`initial_value=datetime(2020,1,1)`) y PK → incremental como en postgres,
`write_disposition=merge/upsert`. `--full-refresh` usa
`refresh="drop_sources"` (acotado a `mssql_*`; NUNCA `pipe.drop()`, que
borraría las tablas postgres del mismo dataset). Chequeo previo
`check_source_types()` aborta ante tipo fuera del mapeo.

## Mapeo de tipos verificado (origen → bronze, sin pérdida)

`DECIMAL(18,2)→DECIMAL(18,2)` (`1.36`, `8888.88` exactos),
`DATETIME2(3)→TIMESTAMP` (ms `.571` preservados), `BIT→BOOLEAN`,
`NVARCHAR→VARCHAR` (unicode `Ñoño ó`, `Ü`, `Ñandú` intacto),
`BIGINT/INT→BIGINT`. Collation origen real: `SQL_Latin1_General_CP1_CI_AS`.
Sin `VARBINARY` en la prueba (límite a documentar si aparece en el real).

## Validación 4.2 (DB `RestoredProof`, mismo lake S3)

| Corrida | bronze tras corrida | Lectura |
|---|---|---|
| 1 inicial | `mssql_productos 150, mssql_ventas 500, mssql_movimientos 350` (= origen) | carga completa OK |
| delta en origen (3 updates + 5 inserts, `updated_at=SYSUTCDATETIME()`) | origen `151 / 502 / 352` | — |
| 2 post-delta | `151 / 502 / 352` (= origen); upserts `SKU-0001→99.99`, `ventas#1→8888.88`, `mov#1→9999`; 0 dupes por PK; sumas `precio 5671.94 / monto 76119.37 / cantidad 9868` = origen; 2 `_dlt_load_id` distintos | solo delta |
| 3 reintento sin cambios | `151 / 502 / 352` (igual, sin nuevo `_dlt_load_id` en filas) | idempotente |
| postgres tras todo | `100 / 50 / 200 / 1309` (igual) + `pipeline.py` corre OK | sin pisar |

```bash
uv pip install -p bronze/.venv/bin/python -r bronze/requirements.txt
bronze/.venv/bin/python bronze/pipeline_mssql.py [--full-refresh] [--tables productos,ventas]
```

---

# Ingesta bronze MSSQL — .bak REAL `FAF12_Kavos` (slice `mssql-bak-source` 3.2 + 5.1)

Carga real contra la DB restaurada del backup del operador (2026-10-15).
No pisa las tablas de prueba (`mssql_productos/ventas/movimientos`) ni las
de postgres: las tablas reales caen en `bronze.mssql_bs_*` /
`bronze.mssql_d_mart_*` (dlt snake_casea los nombres) y el estado dlt va en
el pipeline separado `bronze_mssql_faf12`.

## Ubicación del .bak y restore

- Origen: `/home/mmediza/Descargas/FAF12_Kavos_2026_10_07_233301_411720.bak`
  (7 498 455 552 B). Copiado a `./mssql-backup/` (gitignored) con `cp` y
  tamaño verificado idéntico; dentro del contenedor quedó legible con
  `docker compose exec -T --user root mssql chmod 644
  /var/opt/mssql/backup/FAF12_Kavos_2026_10_07_233301_411720.bak`
  (el motor corre como `mssql`; el `chmod` sin `--user root` da
  `Operation not permitted`).
- `RESTORE HEADERONLY`: DB `FAF12_Kavos`, `SoftwareVersionMajor=15`
  (SQL Server 2019, build 15.0.4322), backup full del 2026-10-07 23:33:02
  desde `ip-172-31-28-103`, collation `Modern_Spanish_CI_AS`, recovery
  SIMPLE. Servidor local `16.0.4295.3` (2022-CU27, major 16 ≥ 15) → **sin
  bloqueo de versión**.
- Comando exacto:
  `./scripts/restore_mssql.sh FAF12_Kavos_2026_10_07_233301_411720.bak FAF12_Kavos`
  → `FAF12_Kavos` ONLINE, 779 tablas de usuario
  (`FAF_XRM12_Data.mdf` 7.6 G + log 1.8 M; ~15 G de disco totales
  entre copia y datafiles, 176 G libres restantes).

## Estrategia por tabla (decisión 3.2)

Perfil de las 779 tablas (`sys.tables`/`sys.columns`/`sys.indexes`):
169 sin PK; **0 columnas `rowversion`** en toda la DB; solo 14 tablas con
nombres tipo cursor (`FechaUltimaModificacion`, etc.); 540 tablas sin
ninguna columna datetime. Conclusión: **ninguna tabla califica para
incremental fiable** — el caso testigo `BSTransaccion.FechaUltimaModificacion`
tiene 15 % de NULLs (48 946/320 447; un incremental por cursor perdería esas
filas ya en la carga inicial). Se aplica el fallback del design (decisión 5):
full-extract diario + `merge`/upsert por PK (el `.bak` es snapshot completo,
converge sin duplicados); `replace` donde no hay PK.

| Origen (`dbo`) | Filas | PK | Estrategia | Bronze |
|---|---|---|---|---|
| `BSMoneda` | 17 | `MonedaID` | merge | `mssql_bs_moneda` |
| `BSProducto` | 9 342 | `ProductoID` | merge | `mssql_bs_producto` |
| `BSOrganizacion` | 3 055 | `OrganizacionID` | merge | `mssql_bs_organizacion` |
| `BSTransaccion` | 320 447 | `TransaccionID` | merge | `mssql_bs_transaccion` |
| `BSOperacion` | 148 356 | `TransaccionID` | merge | `mssql_bs_operacion` |
| `DMart_Ventas` | 275 | — | replace | `mssql_d_mart_ventas` |
| `DMart_VentasProductoCliente` | 4 065 | — | replace | `mssql_d_mart_ventas_producto_cliente` |

Soporte en `pipeline_mssql.py` (sin romper el default de prueba):
env `MSSQL_TABLES="Tab:pk:estrategia,..."`
(`merge` default con pk, `replace` default sin pk, `incremental` legacy solo
con `updated_at`) + `MSSQL_PIPELINE_NAME` para estado dlt separado.
Comando real usado:

```bash
export MSSQL_DATABASE=FAF12_Kavos MSSQL_PIPELINE_NAME=bronze_mssql_faf12 \
  MSSQL_TABLES="BSMoneda:MonedaID:merge,BSProducto:ProductoID:merge,BSOrganizacion:OrganizacionID:merge,BSTransaccion:TransaccionID:merge,BSOperacion:TransaccionID:merge,DMart_Ventas::replace,DMart_VentasProductoCliente::replace"
bronze/.venv/bin/python bronze/pipeline_mssql.py
```

## Mapeo de tipos observado (real, sin pérdida)

`MONEY→DECIMAL(38,9)` (sumas al centavo: `BSTransaccion.ImporteTotal`
538 526 382 190.32, DMart 92 642 264 792.19, `BSProducto.CostoStandard`
1 653 385 342.3758 = origen), `DATETIME→TIMESTAMP` (ms `.553` preservados;
rango `BSTransaccion.FechaAlta` 2018-08-02…2026-10-07), `BIT→BOOLEAN`,
`VARCHAR/NVARCHAR→VARCHAR`, `INT/BIGINT/SMALLINT`, `NUMERIC`, `DATE`.
`TEXT→VARCHAR` (`BSTransaccion.ExportacionObservacion`; 100 % NULL en este
snapshot, mapeo validado estructuralmente). Warning benigno de dlt
(`MONEY ... passed to the normalizer`) sin efecto: valores exactos.
Tipos presentes NO ingeridos (fuera del subset, abortarían en
`check_source_types` hasta ampliar el mapeo): legacy `IMAGE` en
`BSLugarImagen.Imagen`, `FAFDocumentoDigitalizado(2).objeto` y
`dtproperties.lvalue` (diagramas SSMS); `TEXT/NTEXT` en ~20 tablas más.

## Validación real (2 corridas, 2026-10-15)

| Corrida | bronze tras corrida | Lectura |
|---|---|---|
| 1 inicial | `17 / 9342 / 3055 / 320447 / 148356 / 275 / 4065` (= origen, total 485 557) | carga completa OK; 0 dupes por PK en las 5 merge |
| 2 sin cambios | idénticos conteos y sumas | converge sin duplicados (merge re-escribe, `distinct _dlt_load_id`=1) |
| regresión | postgres `100/50/200/1309` y `mssql_productos/ventas/movimientos 151/502/352` intactos | sin pisar nada existente |

---

# Path rápido MSSQL: bcp → staging → MERGE (slice `mssql-fast-ingest`)

Script `bronze/fast_mssql.py`. Declarado **principal en las 7 tablas** del
subset (2026-10-16, speedup 5,4x con paridad total; ver
`docs/findings-mssql-fast-ingest.md`). `pipeline_mssql.py` queda intacto como
fallback/rollback (re-correrlo converge por merge).

## Comandos (con `MSSQL_DATABASE=FAF12_Kavos` + password en `.env`)

```bash
export MSSQL_DATABASE=FAF12_Kavos MSSQL_TABLES="BSMoneda:MonedaID:merge,BSProducto:ProductoID:merge,BSOrganizacion:OrganizacionID:merge,BSTransaccion:TransaccionID:merge,BSOperacion:TransaccionID:merge,DMart_Ventas::replace,DMart_VentasProductoCliente::replace"
bronze/.venv/bin/python bronze/fast_mssql.py all --full          # diario full (12 s)
bronze/.venv/bin/python bronze/fast_mssql.py all                 # diario ventana (BSTransaccion 15 d + resto full)
bronze/.venv/bin/python bronze/fast_mssql.py all --tables BSTransaccion --resync   # resync semanal
bronze/.venv/bin/python bronze/fast_mssql.py parity --ref bronze/fast_ground_truth.json
```

Env: `FAST_MSSQL_PARALLEL` (=nCPUs), `FAST_MSSQL_WINDOW_DAYS` (15),
`FAST_MSSQL_WORKDIR` (`mssql-fast/`, gitignored), `--cutoff YYYY-MM-DD`.

## Supuestos verificados (rompen la paridad si cambian los datos)

- `CHAR(30)` (sentinel de `''`) y `CHAR(31)` (QUOTE del COPY) ausentes en el
  subset; `\t\n\r` saneados solo en columnas sucias (scan previo por corrida).
- `staging_mssql` replica tipos bronze en orden de origen (dlt reordena).
- `_dlt_load_id` = `fast-<UTC>` por corrida, `_dlt_id` = uuid por fila.
- Ventana solo en `BSTransaccion.FechaUltimaModificacion` (`OR IS NULL`);
  `BSOperacion` y chicas siempre full (sin fecha de actividad fiable).
