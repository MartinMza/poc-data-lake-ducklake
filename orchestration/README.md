# Orquestación (Prefect)

Flow único `kavos-lake-medallion` (`orchestration/flow.py`), lineal y de una sola
rama, porque la única fuente de datos es SQL Server:

1. **`bak-watch-restore`** — restore condicional del `.bak`.
2. **`mssql-ingest`** — ingesta bronze (fast path `fast_mssql.py`, o dlt con
   `FAST_MSSQL_MODE=dlt`).

Fail-stop: si el restore falla, la ingesta no corre. Cada `@task` tiene
reintentos configurables y logs centralizados en Prefect.

## Archivos

| Archivo | Qué es |
|---|---|
| `flow.py` | Flow + 2 tasks (`bak-watch-restore`, `mssql-ingest`); entrypoint único |
| `requirements.txt` | `prefect==3.8.8` (cliente) |
| `.venv/` | Venv propio solo con prefect (no commitear; `uv venv`) |
| `.last_restore.json` | Marca del último restore exitoso (path+mtime+size+DB); local, gitignored |

Cada etapa corre como subproceso con el venv ya verificado de su componente
(`bronze/.venv`); este venv solo aporta el cliente Prefect.

## Configuración (env)

El subproceso hereda `os.environ`, así que toda la config se parametriza sin
tocar código (defaults = `docker-compose.yml` / `.env.example`):

| Var | Default | Nota |
|---|---|---|
| `PREFECT_API_URL` | `http://localhost:4200/api` | sin setear, el flow corre efímero |
| `MSSQL_BAK_PATH` | *(sin definir)* | `.bak`/`.tar` a vigilar; sin definir, el restore se salta con warning |
| `MSSQL_DATABASE` | `FAF12_Kavos` | DB destino del restore y origen de la ingesta |
| `MSSQL_TABLES` | las 7 tablas reales | subset `Tabla:pk:estrategia,...` |
| `FAST_MSSQL_MODE` | `fast` | `fast` = bcp→staging→MERGE; `dlt` = fallback |
| `FAST_MSSQL_WINDOW_DAYS` | `15` | ventana incremental de `BSTransaccion` |
| `BRONZE_PYTHON` | `bronze/.venv/bin/python` | intérprete de la ingesta |
| `ORCH_TASK_RETRIES` / `ORCH_TASK_RETRY_DELAY_SECONDS` | `2` / `30` | reintentos por tarea |
| `ORCH_RESTORE_MARK` | `orchestration/.last_restore.json` | marca del último restore |
| `ORCH_BAK_SETTLE_SECONDS` | `5` | espera entre las dos lecturas size/mtime (anti-copia-parcial) |

## Uso

```bash
uv venv orchestration/.venv --python 3.12
uv pip install -p orchestration/.venv/bin/python -r orchestration/requirements.txt

# Día completo (restore si hay .bak nuevo + ingesta MSSQL):
PREFECT_API_URL=http://localhost:4200/api \
MSSQL_BAK_PATH="mssql-backup/<dump-del-dia>.bak" MSSQL_DATABASE=FAF12_Kavos \
  orchestration/.venv/bin/python orchestration/flow.py

# Carga completa (pasa --full al fast path o --full-refresh a dlt)
  orchestration/.venv/bin/python orchestration/flow.py --full-refresh
```

Exit 0 + estado `Completed` si todo pasa; exit 1 + `Failed` con el error de la
etapa que falló.

## Schedule diario y rollback

El `.bak` real llega con nombre rotado por fecha
(`FAF12_Kavos_2026_10_07_233301_411720.bak`), así que `MSSQL_BAK_PATH` debe
apuntar al archivo del día en cada corrida (o a un symlink de nombre estable
que el operador actualice). Sugerido: cron diario después del horario de
llegada del dump (ej. `30 6 * * *` con las env de arriba). A futuro, un
deployment Prefect con schedule cron equivalente.

Rollback: `git revert` de `orchestration/`. Si un restore pisó la DB con el
`.bak` equivocado, re-correr con `MSSQL_BAK_PATH=<bak-correcto>` (el restore es
idempotente con `REPLACE`) o borrar `orchestration/.last_restore.json` para
forzar el restore en la próxima corrida.

## Estado de validación

Histórico (2026-10-16, stack local antes de retirar el origen sintético):

1. **Restore con .bak nuevo** (run `crystal-termite`, Completed): restore
   idempotente (`[FAF12_Kavos] ONLINE`) + marca creada + ingesta fast de las 7
   tablas.
2. **Sin .bak nuevo** (run `favorite-puma`, Completed): el restore se salta y
   la ingesta corre igual (`ALL total: 6.5s`).
3. **.bak nuevo chico + fallback dlt + copia externa** (runs `adept-cougar`,
   `conscious-muskrat`, Completed): restore a DB scratch + `mssql-ingest[dlt]`.
4. **Fallo** (run `skinny-rabbit`, Failed): password MSSQL errónea →
   `mssql-ingest[fast]` Failed tras `Retry 1/2`, `Retry 2/2`
   (`Login failed for user 'sa'`).

Las ramas paralelas `bronze-ingest` (postgres), `dbt-run` y `dbt-test` que
aparecen en esos runs fueron retiradas del flow junto con el origen sintético.
