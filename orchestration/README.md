# Orquestación (Prefect)

Slice `orchestration` del change `poc-data-lake-ducklake` (tasks 6.1–6.2,
spec `orchestration`) + change `mssql-prefect-flow` (tasks 1.1–1.2, specs
`bak-watch-restore` y `mssql-flow-branch`).

Flow único `dulse-medallion` (`orchestration/flow.py`): primero
**`bak-watch-restore`** (restore condicional del `.bak` si hay uno nuevo),
luego las ramas **`bronze-ingest`** (postgres, dlt) y **`mssql-ingest`**
(fast path, default) **en paralelo**, y como join **`dbt run`** →
**`dbt test`** (solo corren si ambas ramas terminan en éxito), con parada
ante el primer fallo (las etapas siguientes no se ejecutan), reintentos
por tarea y logs centralizados en Prefect.

## Archivos

| Archivo | Qué es |
|---|---|
| `flow.py` | Flow + 5 tasks (`bak-watch-restore`, `bronze-ingest`, `mssql-ingest`, `dbt-run`, `dbt-test`); entrypoint único |
| `requirements.txt` | `prefect==3.8.8` (matriz compat §1.3; ver nota de versión abajo) |
| `.venv/` | Venv propio solo con prefect (no commitear; crear con `uv venv`) |
| `.last_restore.json` | Marca del último restore exitoso (path+mtime+size+DB); local, gitignored, la escribe el flow |

No se pisa `bronze/`, `dbt/`, `source/`, `docker-compose.yml` ni versiones
de infra. Cada etapa corre como subproceso con el venv ya verificado de su
slice (`bronze/.venv`, `dbt/.venv`); este venv solo aporta el cliente Prefect.

## Versiones

- `prefect==3.8.8` = pin de `specs/compat-matrix.md` §1.3.
- Nota: el `prefect-server` del compose corre `3.6.22` (fuera del alcance de
  este slice tocarlo). El cliente 3.8.8 opera contra él mostrando un warning
  (`server is running an older version...`); verificado que los flow runs
  quedan tracked y visibles en la API/UI sin otro efecto.

## Configuración (env)

El subproceso hereda `os.environ`, por lo que toda la config se parametriza
sin tocar código (defaults = mismos que `docker-compose.yml`/`.env.example`):

| Var | Default | Nota |
|---|---|---|
| `PREFECT_API_URL` | `http://localhost:4200/api` | sin setear, el flow corre en modo efímero (no se registra en el server) |
| `CATALOG_*` (`HOST`/`PORT`/`DB`/`USER`/`PASSWORD`) | `localhost:5433/ducklake_catalog`, `ducklake` | catálogo DuckLake (ver `bronze/README.md`) |
| `SOURCE_*` | `localhost:5434/source_db`, `source_user` | origen simulado; apuntar a puerto muerto simula "origen caído" |
| `GARAGE_S3_*`, `DUCKLAKE_DATA_PATH` | endpoint `http://localhost:3900`, `s3://lakehouse/data/` | storage del lake |
| `BRONZE_PYTHON` / `DBT_BIN` | `bronze/.venv/bin/python`, `dbt/.venv/bin/dbt` | intérpretes de cada etapa |
| `DBT_PROJECT_DIR` / `DBT_PROFILES_DIR` | `dbt/` | proyecto y perfil dbt |
| `ORCH_TASK_RETRIES` / `ORCH_TASK_RETRY_DELAY_SECONDS` | `2` / `30` | reintentos y espera por tarea |
| `MSSQL_BAK_PATH` | *(no definida)* | `.bak` a vigilar; sin definir, `bak-watch-restore` se salta con warning y la ingesta MSSQL usa la DB ya restaurada |
| `MSSQL_DATABASE` | `RestoredProof` (default de `pipeline_mssql.py`) | DB destino del restore y origen de la ingesta MSSQL (ej. `FAF12_Kavos` para el dato real) |
| `MSSQL_TABLES` | subset según pipeline (`"Nombre:pk:merge,..."`) | tablas de la ingesta MSSQL (fast y dlt lo leen; ver `bronze/README.md`) |
| `FAST_MSSQL_MODE` | `fast` | `fast` = `fast_mssql.py all` (bcp→staging→MERGE); `dlt` = `pipeline_mssql.py` (fallback) |
| `FAST_MSSQL_WINDOW_DAYS` | `15` | ventana incremental de `BSTransaccion` en fast path (el resto va full) |
| `ORCH_RESTORE_MARK` | `orchestration/.last_restore.json` | marca path+mtime+size+DB del último restore exitoso |
| `ORCH_BAK_SETTLE_SECONDS` | `5` | espera entre las dos lecturas size/mtime (anti-copia-parcial) |

`flow.py` acepta además `--tables` y `--full-refresh` (passthrough a
`bronze/pipeline.py`).

## Uso

```bash
uv venv orchestration/.venv --python 3.12
uv pip install -p orchestration/.venv/bin/python -r orchestration/requirements.txt

# Día completo (restore si hay .bak nuevo + ambas ingestas + dbt):
PREFECT_API_URL=http://localhost:4200/api \
MSSQL_BAK_PATH="mssql-backup/<dump-del-dia>.bak" MSSQL_DATABASE=FAF12_Kavos \
  orchestration/.venv/bin/python orchestration/flow.py
# exit 0 + estado Completed si todo pasa; exit 1 + Failed si alguna rama cae
# (dbt no corre si una ingesta falla).
```

## Schedule diario sugerido y rollback

El `.bak` real llega con nombre rotado por fecha
(`FAF12_Kavos_2026_10_07_233301_411720.bak`), así que `MSSQL_BAK_PATH`
debe apuntar al archivo del día en cada corrida (o usar un symlink de
nombre estable que el operador actualice al acercar el dump a
`./mssql-backup/`). Sugerido: cron diario después del horario de llegada
del dump (ej. `30 6 * * *`, con las env de arriba + `FAST_MSSQL_MODE=fast`);
trigger manual con el mismo comando para reprocesos. A futuro, si se crea
un deployment Prefect, usar schedule cron equivalente.

Rollback: la extensión es aditiva sobre `orchestration/` (no toca
`bronze/`, `dbt/`, compose ni servicios). Volver al flow anterior =
`git revert` de `orchestration/` (+ redeploy/re-corridas). Si un restore
pisó la DB con el `.bak` equivocado, re-correr con
`MSSQL_BAK_PATH=<bak-correcto>` (el restore es idempotente con REPLACE)
o borrar `orchestration/.last_restore.json` para forzar el restore en la
próxima corrida.

# Prueba de fallo (origen caído -> dbt no corre, flow Failed con el error origen):
PREFECT_API_URL=http://localhost:4200/api SOURCE_PORT=5499 \
  ORCH_TASK_RETRY_DELAY_SECONDS=5 \
  orchestration/.venv/bin/python orchestration/flow.py
```

Flows visibles en el server (ejemplo):

```bash
curl -s -X POST http://localhost:4200/api/flow_runs/filter \
  -H 'Content-Type: application/json' \
  -d '{"flows":{"name":{"any_":["dulse-medallion"]}},"limit":10}'
```

## Estado de validación (2026-10-08, stack local en verde)

1. **Punta a punta**: `flow dulse-medallion: Completed` (exit 0). Orden
   verificado: `bronze-ingest` Completed (`100/50/200/1309`, total 1659 =
   estado del lake, 0 filas nuevas, incremental sin duplicados) →
   `dbt run` 7/7 OK → `dbt test` 28/28 PASS. Run `olive-bison` en el server
   con sus 3 task runs en Completed.
2. **Fallo**: con `SOURCE_PORT=5499` (connection refused),
   `flow dulse-medallion: Failed` (exit 1) con el `OperationalError` del
   origen; `dbt run`/`dbt test` nunca se ejecutaron (0 menciones en el log,
   sin task runs en el server). Reintentos visibles: `Retry 1/2`, `Retry 2/2`
   (3 intentos con logs por intento) antes del Failed final. Run
   `advanced-boar`: solo el task `bronze-ingest` en Failed.
3. **Visibilidad**: ambos runs consultables por API (`/api/flow_runs/filter`,
   `/api/task_runs/filter`) y con URL de detalle
   (`/runs/flow-run/<id>`); logs de cada etapa volcados al log del task.

## Estado de validación `mssql-prefect-flow` (2026-10-16, stack local en verde)

Rama MSSQL con `MSSQL_DATABASE=FAF12_Kavos` (dato real, 7 tablas fast) y
postgres en paralelo; todos los runs registrados en prefect-server local
(`:4200`, sin deployments: tracking por `PREFECT_API_URL`):

1. **Restore con .bak nuevo** (run `crystal-termite`, Completed, exit 0):
   sin marca previa, el `.bak` real
   (`FAF12_Kavos_2026_10_07_233301_411720.bak`, 7 498 455 552 B) disparó
   restore idempotente (`[FAF12_Kavos] ONLINE`) y creó
   `.last_restore.json` (path+size+mtime_ns+DB). 5/5 tasks Completed en el
   server; fast `all` + postgres + `dbt run` 7/7 + `dbt test` 28/28.
2. **Sin .bak nuevo** (run `favorite-puma`, Completed, exit 0):
   `bak-watch-restore` Completed con "sin .bak nuevo (...); se salta el
   restore" (sin tocar la instancia); ramas en paralelo
   (fast `ALL total: 6.5s`, postgres `TOTAL bronze: 1659 filas`) + dbt
   7/7 y 28/28 en verde.
3. **.bak nuevo chico + fallback dlt + copia externa** (runs
   `adept-cougar` y `conscious-muskrat`, ambos Completed, exit 0):
   copia de `test-source.bak` con otro nombre → restore a DB scratch
   (`[ScratchRestoreTest]` / `[ScratchRestoreTest2]` ONLINE) + marca
   actualizada; `mssql-ingest[dlt]` (`TOTAL bronze (mssql): 1005 filas`) +
   dbt verde. La copia externa (`/tmp/...` → `./mssql-backup/`) quedó
   verificada en `conscious-muskrat`. Artefactos de prueba limpiados
   (DBs scratch dropeadas, copias y marca eliminadas).
4. **Fallo MSSQL** (run `skinny-rabbit`, Failed, exit 1):
   sin `MSSQL_BAK_PATH` (`bak-watch-restore` Completed con warning) +
   password MSSQL errónea → `mssql-ingest[fast]` Failed tras
   `Retry 1/2`, `Retry 2/2` (`Login failed for user 'sa'`);
   `bronze-ingest` Completed; **cero** task runs ni menciones de
   `dbt-run`/`dbt-test`; flow Failed con el `RuntimeError` de la rama
   origen. Redeploy = corridas trackeadas en el server local
   (verificable por `/api/flow_runs/filter` + `/api/task_runs/filter`).

Hallazgo para el schedule: el `.bak` real llega con nombre rotado por
fecha, por lo que `MSSQL_BAK_PATH` debe actualizarse en cada corrida
(ver "Schedule diario sugerido" arriba).

## Notas para el grupo 7

- Este flow es la base para backup/benchmark/hallazgos: reusar
  `BRONZE_PYTHON`/`DBT_BIN` o añadir tasks nuevas con el mismo patrón
  (subproceso + `_run` + fail-stop por excepción).
- Deuda conocida (no de este slice): `dbt/profiles.yml` lleva hosts/puertos
  fijos (localhost:5433, endpoint S3) en vez de `env_var()`; la config por
  env del flow hoy cubre bronze + paths dbt. Si el grupo 7 necesita el lake
  en otro host, evaluar `DBT_PROFILES_DIR` alternativo o `env_var()` en el
  perfil (implica tocar `dbt/`, coordinar con su dueño).
