#!/usr/bin/env python3
"""Flow Prefect: restore condicional (.bak) -> ingestas bronze -> `dbt run` -> `dbt test`.

OpenSpec `poc-data-lake-ducklake`, tareas 6.1-6.2 (spec `orchestration`)
+ OpenSpec `mssql-prefect-flow` (specs `bak-watch-restore`, `mssql-flow-branch`).

- `bak-watch-restore` (primera): compara el `.bak` de `MSSQL_BAK_PATH`
  contra la marca JSON del último restore exitoso
  (`orchestration/.last_restore.json`: path+mtime+size+DB); si no hay
  `.bak` nuevo se salta en éxito. Sin `MSSQL_BAK_PATH` se salta con
  warning (operador solo-postgres). Doble lectura con espera breve
  anti-copia-parcial. Si hay `.bak` nuevo lo copia a `./mssql-backup/`,
  lo restaura con `scripts/restore_mssql.sh` y actualiza la marca SOLO
  en éxito.
- Ramas en paralelo: `bronze-ingest` (postgres, dlt) y `mssql-ingest`
  (fast path `fast_mssql.py`, default; `FAST_MSSQL_MODE=dlt` usa
  `pipeline_mssql.py`) corren concurrentes; `dbt run`/`dbt test` actúan
  como join: solo corren si ambas ramas terminan en éxito (fail-stop:
  si una rama lanza, dbt no se ejecuta y el flow queda Failed con el
  error de la rama origen).
- Reintentos por tarea: cada `@task` lleva `retries` + `retry_delay_seconds`
  (configurables por env). Los intentos y sus logs quedan en Prefect.
- Sin duplicar entornos: cada etapa se ejecuta como subproceso con el venv
  ya verificado de su slice (`bronze/.venv` con dlt==1.31.0, `dbt/.venv`
  con dbt-core==1.12.5/dbt-duckdb==1.11.0). Este venv solo aporta prefect.
- Config por env: el subproceso hereda `os.environ`, por lo que toda la
  config de `bronze/pipeline.py` (CATALOG_*, SOURCE_*, GARAGE_*,
  DUCKLAKE_*), `bronze/fast_mssql.py` / `bronze/pipeline_mssql.py`
  (MSSQL_*, FAST_MSSQL_*) y `PREFECT_API_URL` se parametriza sin tocar código.
  Solo datos sintéticos (origen simulado). Sin credenciales en el repo.

Uso:
    orchestration/.venv/bin/python orchestration/flow.py [--tables ...] [--full-refresh]
    # PREFECT_API_URL=http://localhost:4200/api para trackear en prefect-server.

Entrypoint único y documentado: `python orchestration/flow.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from prefect import flow, get_run_logger, task

ROOT = Path(__file__).resolve().parent.parent

BRONZE_PYTHON = os.getenv("BRONZE_PYTHON", str(ROOT / "bronze" / ".venv" / "bin" / "python"))
DBT_BIN = os.getenv("DBT_BIN", str(ROOT / "dbt" / ".venv" / "bin" / "dbt"))
DBT_PROJECT_DIR = os.getenv("DBT_PROJECT_DIR", str(ROOT / "dbt"))
DBT_PROFILES_DIR = os.getenv("DBT_PROFILES_DIR", str(ROOT / "dbt"))

TASK_RETRIES = int(os.getenv("ORCH_TASK_RETRIES", "2"))
TASK_RETRY_DELAY_SECONDS = int(os.getenv("ORCH_TASK_RETRY_DELAY_SECONDS", "30"))

# Marca del último restore exitoso (path+mtime+size+DB). Local y gitignored
# (ver orchestration/.gitignore); se actualiza solo tras restore + verificación OK.
RESTORE_MARK_PATH = Path(os.getenv(
    "ORCH_RESTORE_MARK",
    str(ROOT / "orchestration" / ".last_restore.json")))
# Espera entre las dos lecturas de size/mtime (anti-copia-parcial).
BAK_SETTLE_SECONDS = int(os.getenv("ORCH_BAK_SETTLE_SECONDS", "5"))

FLOW_NAME = "dulse-medallion"


def _run(cmd: list[str], stage: str) -> str:
    """Ejecuta `cmd`, vuelca su salida al log de Prefect y falla si rc != 0."""
    logger = get_run_logger()
    logger.info("%s: ejecutando: %s", stage, shlex.join(cmd))
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=os.environ.copy(),  # config por env (hosts, credenciales de prueba)
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(f"===== {stage} (rc={proc.returncode}) =====")
    print(proc.stdout)
    sys.stdout.flush()
    if proc.returncode != 0:
        tail = "\n".join(proc.stdout.splitlines()[-15:])
        raise RuntimeError(f"{stage} falló con rc={proc.returncode}. Cola:\n{tail}")
    return proc.stdout


@task(name="bronze-ingest", retries=TASK_RETRIES,
      retry_delay_seconds=TASK_RETRY_DELAY_SECONDS, log_prints=True)
def bronze_ingest(tables: str | None = None, full_refresh: bool = False) -> str:
    """Rama postgres: pipeline dlt postgres-source -> bronze en DuckLake."""
    cmd = [BRONZE_PYTHON, "bronze/pipeline.py"]
    if tables:
        cmd += ["--tables", tables]
    if full_refresh:
        cmd += ["--full-refresh"]
    return _run(cmd, "bronze-ingest")


def _stat_sig(path: Path) -> tuple[int, int]:
    """Firma (size, mtime_ns) de un archivo."""
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


def _resolve_restore_target(bak_name: str) -> str:
    """DB destino del restore: `MSSQL_DATABASE` si está definida, si no el
    default de `scripts/restore_mssql.sh` (basename del .bak sanitizado)."""
    db = os.getenv("MSSQL_DATABASE", "").strip()
    if db:
        return db
    stem = bak_name[:-4] if bak_name.lower().endswith(".bak") else bak_name
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in stem)


def _read_mark() -> dict | None:
    try:
        with open(RESTORE_MARK_PATH, encoding="utf-8") as fh:
            mark = json.load(fh)
        if isinstance(mark, dict):
            return mark
    except (OSError, ValueError):
        pass
    return None


@task(name="bak-watch-restore", retries=TASK_RETRIES,
      retry_delay_seconds=TASK_RETRY_DELAY_SECONDS, log_prints=True)
def bak_watch_restore() -> str:
    """Etapa 0: restore condicional del .bak indicado por `MSSQL_BAK_PATH`.

    Sin variable configurada termina en éxito con warning (operador
    solo-postgres). Con `.bak` sin cambios respecto a la marca termina en
    éxito sin tocar la instancia. La marca se actualiza solo en éxito.
    """
    logger = get_run_logger()
    bak_raw = os.getenv("MSSQL_BAK_PATH", "").strip()
    if not bak_raw:
        msg = ("bak-watch-restore: MSSQL_BAK_PATH no definida; se salta el "
               "restore (la ingesta MSSQL usa la DB ya restaurada)")
        logger.warning(msg)
        print(f"WARNING: {msg}", flush=True)
        return "skipped: sin MSSQL_BAK_PATH"
    src = Path(bak_raw).expanduser()
    if not src.is_absolute():
        src = (ROOT / src).resolve()
    if not src.is_file():
        raise RuntimeError(
            f"bak-watch-restore: no existe {src} (MSSQL_BAK_PATH mal definida)")
    size1, mtime1 = _stat_sig(src)
    time.sleep(BAK_SETTLE_SECONDS)
    size2, mtime2 = _stat_sig(src)
    if (size1, mtime1) != (size2, mtime2):
        msg = (f"bak-watch-restore: {src.name} cambió durante la espera "
               f"({size1}->{size2} B); posible copia parcial, se salta el restore")
        logger.warning(msg)
        print(f"WARNING: {msg}", flush=True)
        return "skipped: copia parcial sospechada"
    target = _resolve_restore_target(src.name)
    current = {"bak_path": str(src), "size": size2,
               "mtime_ns": mtime2, "db": target}
    mark = _read_mark()
    if mark == current:
        msg = (f"bak-watch-restore: sin .bak nuevo ({src.name}, "
               f"{size2} B, DB [{target}]); se salta el restore")
        logger.info(msg)
        print(msg, flush=True)
        return "skipped: sin .bak nuevo"
    # .bak nuevo (nombre, tamaño, mtime o DB destino distintos de la marca).
    backup_dir = ROOT / "mssql-backup"
    backup_dir.mkdir(exist_ok=True)
    dest = backup_dir / src.name
    if dest.exists() and os.path.samefile(src, dest):
        msg = f"bak-watch-restore: {src.name} ya está en ./mssql-backup/, sin copia"
        logger.info(msg)
        print(msg, flush=True)
    else:
        shutil.copy2(src, dest)
        msg = f"bak-watch-restore: copiado {src} -> {dest} ({size2} B)"
        logger.info(msg)
        print(msg, flush=True)
    cmd = ["bash", "scripts/restore_mssql.sh", dest.name]
    if os.getenv("MSSQL_DATABASE", "").strip():
        cmd += [target]
    out = _run(cmd, "bak-watch-restore")
    RESTORE_MARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESTORE_MARK_PATH, "w", encoding="utf-8") as fh:
        json.dump(current, fh, indent=1)
    logger.info("bak-watch-restore: marca actualizada en %s", RESTORE_MARK_PATH)
    return f"restored {dest.name} -> [{target}]"


@task(name="mssql-ingest", retries=TASK_RETRIES,
      retry_delay_seconds=TASK_RETRY_DELAY_SECONDS, log_prints=True)
def mssql_ingest(full_refresh: bool = False) -> str:
    """Rama MSSQL: fast path (`fast_mssql.py all`) por default, dlt
    (`pipeline_mssql.py`) con `FAST_MSSQL_MODE=dlt`. Tablas/ventana por env
    (`MSSQL_TABLES`, `FAST_MSSQL_WINDOW_DAYS`); hereda el resto de `os.environ`."""
    mode = os.getenv("FAST_MSSQL_MODE", "fast").strip().lower() or "fast"
    if mode == "fast":
        cmd = [BRONZE_PYTHON, "bronze/fast_mssql.py", "all"]
        if full_refresh:
            cmd += ["--full"]
    elif mode == "dlt":
        cmd = [BRONZE_PYTHON, "bronze/pipeline_mssql.py"]
        if full_refresh:
            cmd += ["--full-refresh"]
    else:
        raise RuntimeError(
            f"mssql-ingest: FAST_MSSQL_MODE={mode!r} inválido (fast|dlt)")
    return _run(cmd, f"mssql-ingest[{mode}]")


@task(name="dbt-run", retries=TASK_RETRIES,
      retry_delay_seconds=TASK_RETRY_DELAY_SECONDS, log_prints=True)
def dbt_run() -> str:
    """Etapa 2: `dbt run` (silver + gold sobre el mismo DuckLake)."""
    cmd = [DBT_BIN, "run", "--project-dir", DBT_PROJECT_DIR,
           "--profiles-dir", DBT_PROFILES_DIR]
    return _run(cmd, "dbt-run")


@task(name="dbt-test", retries=TASK_RETRIES,
      retry_delay_seconds=TASK_RETRY_DELAY_SECONDS, log_prints=True)
def dbt_test() -> str:
    """Etapa 3: `dbt test` (not_null, unique, relationships + singular)."""
    cmd = [DBT_BIN, "test", "--project-dir", DBT_PROJECT_DIR,
           "--profiles-dir", DBT_PROFILES_DIR]
    return _run(cmd, "dbt-test")


@flow(name=FLOW_NAME, log_prints=True)
def dulse_medallion_flow(tables: str | None = None,
                         full_refresh: bool = False) -> dict[str, str]:
    """Día completo: restore condicional, ambas ingestas en paralelo y dbt
    como join (solo corre si las dos ramas terminan en éxito; si una rama
    lanza, dbt no se ejecuta y el flow queda Failed con el error origen)."""
    restore_log = bak_watch_restore()
    pg_future = bronze_ingest.submit(tables=tables, full_refresh=full_refresh)
    ms_future = mssql_ingest.submit(full_refresh=full_refresh)
    bronze_log = pg_future.result()
    mssql_log = ms_future.result()
    run_log = dbt_run()
    test_log = dbt_test()
    return {"bak_watch_restore": restore_log, "bronze_ingest": bronze_log,
            "mssql_ingest": mssql_log, "dbt_run": run_log,
            "dbt_test": test_log}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Flow restore-condicional -> ingestas -> dbt run -> dbt test")
    ap.add_argument("--tables", default=None,
                    help="subset coma-separado para bronze/pipeline.py (postgres)")
    ap.add_argument("--full-refresh", action="store_true",
                    help="pasa --full-refresh a bronze/pipeline.py")
    args = ap.parse_args(argv)
    state = dulse_medallion_flow(tables=args.tables,
                                full_refresh=args.full_refresh,
                                return_state=True)
    print(f"flow {FLOW_NAME}: {state.name}")
    return 0 if state.is_completed() else 1


if __name__ == "__main__":
    sys.exit(main())
