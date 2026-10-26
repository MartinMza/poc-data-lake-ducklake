#!/usr/bin/env python3
"""Flow Prefect: restore condicional del .bak -> ingesta bronze desde SQL Server.

Feature `api-auth-mssql-only`; el lago quedó con una única fuente (SQL Server).
El origen Postgres sintético, las ramas en paralelo y las etapas dbt fueron
retirados: el flujo es lineal y de una sola rama.

- `bak-watch-restore` (primera): compara el `.bak` de `MSSQL_BAK_PATH` contra
  la marca JSON del último restore exitoso
  (`orchestration/.last_restore.json`: path+mtime+size+DB); sin `.bak` nuevo
  se salta en éxito. Sin `MSSQL_BAK_PATH` se salta con warning (operador que
  ya restauró a mano). Doble lectura con espera breve anti-copia-parcial. Si
  hay `.bak` nuevo lo copia a `./mssql-backup/`, lo restaura con
  `scripts/restore_mssql.sh` y actualiza la marca SOLO en éxito.
- `mssql-ingest`: fast path `bronze/fast_mssql.py all` (default) o dlt
  `bronze/pipeline_mssql.py` con `FAST_MSSQL_MODE=dlt` (fallback/rollback).
- Reintentos por tarea: cada `@task` lleva `retries` + `retry_delay_seconds`
  (configurables por env). Los intentos y sus logs quedan en Prefect.
- Sin duplicar entornos: cada etapa corre como subproceso con el venv ya
  verificado de su componente (`bronze/.venv`); este venv solo aporta prefect.
- Config por env: el subproceso hereda `os.environ`, así que `MSSQL_*`,
  `FAST_MSSQL_*`, `CATALOG_*`, `GARAGE_*`, `DUCKLAKE_*` y `PREFECT_API_URL` se
  parametrizan sin tocar código.

Uso:
    orchestration/.venv/bin/python orchestration/flow.py [--full-refresh]
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
import tarfile
import time
from pathlib import Path

from prefect import flow, get_run_logger, task

ROOT = Path(__file__).resolve().parent.parent

BRONZE_PYTHON = os.getenv("BRONZE_PYTHON", str(ROOT / "bronze" / ".venv" / "bin" / "python"))

TASK_RETRIES = int(os.getenv("ORCH_TASK_RETRIES", "2"))
TASK_RETRY_DELAY_SECONDS = int(os.getenv("ORCH_TASK_RETRY_DELAY_SECONDS", "30"))

# Marca del último restore exitoso (path+mtime+size+DB). Local y gitignored
# (ver orchestration/.gitignore); se actualiza solo tras restore OK.
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


def _stat_sig(path: Path) -> tuple[int, int]:
    """Firma (size, mtime_ns) de un archivo."""
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")


def _is_tar_archive(name: str) -> bool:
    low = name.lower()
    return any(low.endswith(sfx) for sfx in TAR_SUFFIXES)


def _resolve_restore_target(bak_name: str) -> str:
    """DB destino del restore: `MSSQL_DATABASE` si está definida, si no el
    default de `scripts/restore_mssql.sh` (basename del .bak/.tar sanitizado)."""
    db = os.getenv("MSSQL_DATABASE", "").strip()
    if db:
        return db
    stem = bak_name
    for sfx in (*TAR_SUFFIXES, ".bak"):
        if stem.lower().endswith(sfx):
            stem = stem[: -len(sfx)]
            break
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

    Sin variable configurada termina en éxito con warning (el operador ya
    restauró la base). Con `.bak` sin cambios respecto a la marca termina en
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
    # .bak / .tar nuevo (nombre, tamaño, mtime o DB destino distintos de la marca).
    backup_dir = ROOT / "mssql-backup"
    backup_dir.mkdir(exist_ok=True)
    if _is_tar_archive(src.name):
        with tarfile.open(src, "r:*") as tf:
            bak_members = [m for m in tf.getmembers() if m.isfile() and m.name.lower().endswith(".bak")]
            if not bak_members:
                raise RuntimeError(f"bak-watch-restore: el archivo {src} no contiene ningún .bak")
            member = bak_members[0]
            dest = backup_dir / Path(member.name).name
            msg = f"bak-watch-restore: descomprimiendo {src} ({member.name}) -> {dest}"
            logger.info(msg)
            print(msg, flush=True)
            src_f = tf.extractfile(member)
            if src_f is None:
                raise RuntimeError(f"bak-watch-restore: no se pudo leer {member.name} de {src}")
            with src_f, open(dest, "wb") as out_f:
                shutil.copyfileobj(src_f, out_f)
        os.chmod(dest, 0o644)
        target = _resolve_restore_target(dest.name)
        current["db"] = target
    else:
        dest = backup_dir / src.name
        if dest.exists() and os.path.samefile(src, dest):
            msg = f"bak-watch-restore: {src.name} ya está en ./mssql-backup/, sin copia"
            logger.info(msg)
            print(msg, flush=True)
        else:
            shutil.copy2(src, dest)
            os.chmod(dest, 0o644)
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
    """Ingesta MSSQL: fast path (`fast_mssql.py all`) por default, dlt
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


@flow(name=FLOW_NAME, log_prints=True)
def dulse_medallion_flow(full_refresh: bool = False) -> dict[str, str]:
    """Día completo: restore condicional del .bak y luego la ingesta bronze
    desde SQL Server (fail-stop: si el restore falla, no se ingesta)."""
    restore_log = bak_watch_restore()
    ingest_log = mssql_ingest(full_refresh=full_refresh)
    return {"bak_watch_restore": restore_log, "mssql_ingest": ingest_log}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Flow restore-condicional -> ingesta bronze desde SQL Server")
    ap.add_argument("--full-refresh", action="store_true",
                    help="pasa --full (fast path) o --full-refresh (dlt) a la ingesta")
    args = ap.parse_args(argv)
    state = dulse_medallion_flow(full_refresh=args.full_refresh, return_state=True)
    print(f"flow {FLOW_NAME}: {state.name}")
    return 0 if state.is_completed() else 1


if __name__ == "__main__":
    sys.exit(main())
