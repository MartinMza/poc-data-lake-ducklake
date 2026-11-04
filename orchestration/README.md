# Orquestación (Prefect)

Flow único `kavos-lake-medallion` (`orchestration/flow.py`), lineal y de una sola
rama, porque la única fuente de datos es SQL Server:

1. **`acquire`** — baja el `.bak` nuevo desde Gmail (link S3 presignado) a
   `mssql-backup/`, verificado por checksum. Rechaza un artefacto más viejo que
   el último aplicado. Sin `ACQUIRE_*` configurado se salta en éxito.
2. **`bak-extract`** — extrae el backup completo a Parquet con `bak2parquet`
   (motor efímero acotado en memoria). Toma el archivo que dejó `acquire`.
3. **`bronze-load`** — carga ese Parquet en DuckLake Bronze (`load_parquet.py`).

Fail-stop: si la adquisición o la extracción fallan, la ingesta no corre; si la
carga falla, el backup nunca se marca como aplicado. Cada `@task` tiene
reintentos configurables y logs centralizados en Prefect.

Schedule declarado (sin cron externo): `orchestration/serve.py` sirve el flow
con un cron derivado de `ACQUIRE_POLL_HOURS` (default 4 h).

En el stack, ese scheduler es el servicio **`prefect-serve`**
(`docker compose up -d prefect-serve`, `restart: unless-stopped`). Reusa la
imagen `kavos-lake-pipeline`, así que corre el mismo camino de datos que
`lake-init` (ODBC + venvs pre-construidos), contra `mssql`, `garage` y
`postgres-catalog`. Monta `./mssql-backup` y dos volúmenes: `serve-state`
(estado y marca de extracción) y `serve-parquet`. **Si `serve-state` se
pierde, la guarda de antigüedad olvida el último backup aplicado** y podría
volver a cargar uno viejo.

Con el servicio corriendo no hace falta —ni conviene— lanzar `flow.py` a mano:
es el mismo flow y competirían por el mismo estado.

El aviso real de Finnegans llega por Amazon SES desde
`info@finnegans.com.ar` con asunto `Backup de Base de Datos Disponible`. Esos
dos valores son defaults en `kavos_acquire/defaults.py` —no hace falta
configurarlos— y `tests/test_mailbox.py` los fija contra el correo real. El
`text/plain` del aviso no trae el link: sólo el `text/html`, con el `href` entre
comillas simples y la URL cortada en 8 puntos por quoted-printable.

Dos comportamientos que no son opcionales (requisito del operador):

- **Filtro por fecha**: el poller busca con `IMAP SINCE` una ventana de
  `ACQUIRE_SINCE_DAYS` (default 3, menor que la firma de 12 h). No recorre el
  historial del buzón.
- **Un solo backup**: tras verificar checksum y commitear el artefacto nuevo,
  se borra el anterior (`ACQUIRE_KEEP_BACKUPS`, default 1). La poda automática
  usa la misma implementación que `scripts/prune_backups.py`
  (`kavos_acquire/retention.py`) y nunca corre antes de verificar: borrar el
  anterior para hacerle lugar a uno dudoso sería perder los dos.

## Archivos

| Archivo | Qué es |
|---|---|
| `flow.py` | Flow + 3 tasks (`acquire`, `bak-extract`, `bronze-load`); entrypoint |
| `serve.py` | Sirve el flow con schedule cron (`ACQUIRE_POLL_HOURS`) |
| `kavos_acquire/` | Adquisición: buzón, allowlist, descarga verificada, estado |
| `tests/` | Tests del componente (sin red, sin motor): `pytest` desde acá |
| `requirements.txt` | `prefect==3.8.8` (cliente) + `httpx` + `pytest` |
| `.venv/` | Venv propio solo con prefect (no commitear; `uv venv`) |
| `.last_restore.json` | Marca del último restore exitoso (path+mtime+size+DB); local, gitignored |
| `.acquire_state.json` | UIDs y Message-IDs procesados + último backup aplicado; local, gitignored |

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
| `BAK2PARQUET_BATCH_SIZE` | `50000` | filas por batch Arrow en la extracción |
| `BAK2PARQUET_MAX_TEXT_SIZE` / `BAK2PARQUET_MAX_BINARY_SIZE` | defaults del paquete | cotas de buffers para columnas `MAX` |
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
