#!/usr/bin/env python3
"""Flow Prefect: `.bak` nuevo -> Parquet (`bak-extract`) -> Bronze (`bronze-load`).

El lago quedó con una única fuente (SQL Server vía backup `.bak`) y un solo
camino de datos: el extractor vuelca el backup completo a Parquet y el
cargador lo sube a Bronze con reemplazo total por tabla (idempotente, sin
ventanas ni modos por tabla).

- `acquire` (primera): trae el `.bak` nuevo desde Gmail a `mssql-backup/`
  vía `kavos_acquire.pipeline.acquire_latest_bak`. Sin `ACQUIRE_IMAP_USER` o
  `ACQUIRE_LINK_HOSTS` se salta en éxito con warning (el workflow manual con
  `MSSQL_BAK_PATH` sigue intacto). Un fallo de adquisición falla la corrida
  para que se vea (spec S9).
- `bak-extract` (segunda): compara el `.bak` (el adquirido por `acquire` si
  lo hubo, si no el de `MSSQL_BAK_PATH`) contra la
  marca JSON de la última extracción exitosa
  (`orchestration/.last_restore.json`: path+mtime+size+salida). Sin `.bak`
  nuevo se salta en éxito. Sin `MSSQL_BAK_PATH` se salta con warning (el
  operador ya dejó el Parquet). Doble lectura con espera breve
  anti-copia-parcial. Si hay `.bak` nuevo lo copia a `./mssql-backup/`, corre
  `python -m bak2parquet --engine url` contra la instancia existente y
  actualiza la marca SOLO en éxito. El extractor restaura en una base temporal
  propia y la elimina al terminar: no deja objetos residuales.
- `bronze-load` (tercera): corre `bronze/load_parquet.py --src` sobre la
  salida del extractor (fail-stop: si la extracción falla, no se carga).
  Si la carga termina bien y `acquire` trajo un backup, se registra como
  aplicado (`mark_applied`); si la carga falla nunca se marca.
- Reintentos por tarea: cada `@task` lleva `retries` + `retry_delay_seconds`
  (configurables por env). Los intentos y sus logs quedan en Prefect.
- Sin duplicar entornos: cada etapa corre como subproceso con el venv ya
  verificado de `bronze` (este venv solo aporta prefect y el path del
  extractor vía `PYTHONPATH`). El intérprete de ambas etapas debe tener las
  dependencias del extractor instaladas.
- Config por env: el subproceso hereda `os.environ`, así que `MSSQL_*`,
  `BAK2PARQUET_*`, `CATALOG_*`, `GARAGE_*`, `DUCKLAKE_*` y `PREFECT_API_URL` se
  parametrizan sin tocar código.

Uso:
    orchestration/.venv/bin/python orchestration/flow.py
    # PREFECT_API_URL=http://localhost:4200/api para trackear en prefect-server.

Entrypoint único y documentado: `python orchestration/flow.py`.
Schedule declarado sin cron externo: `python orchestration/serve.py`
(cron derivado de `ACQUIRE_POLL_HOURS`, default cada 4 h).
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

from kavos_acquire.defaults import FINNEGANS_SENDER, FINNEGANS_SUBJECT_PATTERN
from kavos_acquire.download import DEFAULT_MIN_WINDOW_SECONDS
from kavos_acquire.identity import BackupId, BackupNameError, parse_backup_id
from kavos_acquire.retention import DEFAULT_KEEP, newest_backup
from kavos_acquire.state import load_state
from kavos_acquire.pipeline import (
    DEFAULT_SINCE_DAYS,
    STATUS_ACQUIRED,
    AcquireOutcome,
    acquire_latest_bak,
    mark_applied,
)

ROOT = Path(__file__).resolve().parent.parent

BRONZE_PYTHON = os.getenv("BRONZE_PYTHON", str(ROOT / "bronze" / ".venv" / "bin" / "python"))

# Directorio con el `<schema>/<tabla>.parquet` del extractor (lo escribe
# `bak-extract`, lo lee `bronze-load`).
PARQUET_OUT = os.getenv("BAK2PARQUET_OUT", str(ROOT / "parquet-out"))

TASK_RETRIES = int(os.getenv("ORCH_TASK_RETRIES", "2"))
TASK_RETRY_DELAY_SECONDS = int(os.getenv("ORCH_TASK_RETRY_DELAY_SECONDS", "30"))

# Marca de la última extracción exitosa (path+mtime+size+salida). Local y
# gitignored (ver orchestration/.gitignore); se actualiza solo tras éxito.
RESTORE_MARK_PATH = Path(os.getenv(
    "ORCH_RESTORE_MARK",
    str(ROOT / "orchestration" / ".last_restore.json")))
# Espera entre las dos lecturas de size/mtime (anti-copia-parcial).
BAK_SETTLE_SECONDS = int(os.getenv("ORCH_BAK_SETTLE_SECONDS", "5"))

# Ruta del `.bak` vista por el motor (en compose `./mssql-backup` está
# montado en `/var/opt/mssql/backup` del servicio `mssql`).
MOTOR_BACKUP_DIR = "/var/opt/mssql/backup"

FLOW_NAME = "kavos-lake-medallion"

# Estado de la adquisición (Message-IDs, UIDs, último backup aplicado).
# Se lee en el momento de correr (no al importar) para que `serve` de larga
# vida vea cambios de env sin reiniciar.
ACQUIRE_STATE_DEFAULT = str(ROOT / "orchestration" / ".acquire_state.json")
ACQUIRE_INCOMING_DEFAULT = str(ROOT / "mssql-backup" / ".incoming")
ACQUIRE_FINAL_DEFAULT = str(ROOT / "mssql-backup")


def _run(cmd: list[str], stage: str, extra_env: dict[str, str] | None = None) -> str:
    """Ejecuta `cmd`, vuelca su salida al log de Prefect y falla si rc != 0."""
    logger = get_run_logger()
    logger.info("%s: ejecutando: %s", stage, shlex.join(cmd))
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,  # config por env (hosts, credenciales de prueba)
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


def _read_mark() -> dict | None:
    try:
        with open(RESTORE_MARK_PATH, encoding="utf-8") as fh:
            mark = json.load(fh)
        if isinstance(mark, dict):
            return mark
    except (OSError, ValueError):
        pass
    return None


def _extractor_env() -> dict[str, str]:
    """El extractor vive en `bak2parquet/src` (layout src/): se expone por
    `PYTHONPATH` para correrlo con el intérprete de bronze."""
    base = os.environ.get("PYTHONPATH", "")
    path = str(ROOT / "bak2parquet" / "src")
    return {"PYTHONPATH": f"{path}{os.pathsep}{base}" if base else path}


def bak_path_for(
    outcome: AcquireOutcome | dict | None,
    env_path: str,
    final_dir: str | Path | None = None,
    applied: BackupId | None = None,
) -> str:
    """Ruta del `.bak` a extraer, en orden de precedencia.

    1. Lo que trajo `acquire` en esta corrida.
    2. `MSSQL_BAK_PATH` explícito (carga manual del operador).
    3. El artefacto más nuevo que ya esté en `final_dir`.

    El punto 3 no es un lujo: la adquisición se consume una sola vez (avanzan el
    UID y el Message-ID), así que si la extracción falla, sin este fallback el
    archivo descargado queda huérfano y el lago no se actualiza nunca más.
    `bak-extract` ya tiene su propia marca (path+mtime+size), así que reintentar
    sobre lo mismo es barato: se salta si no cambió.

    Pura: no toca red, motor ni Prefect.
    """
    if outcome is not None:
        if isinstance(outcome, dict):
            status, path = outcome.get("status"), outcome.get("path")
        else:
            status, path = outcome.status, outcome.path
        if status == STATUS_ACQUIRED and path:
            return str(path)
    if env_path:
        return env_path
    if final_dir:
        pendiente = newest_backup(final_dir)
        if pendiente is not None:
            return str(pendiente) if _puede_ingerirse(pendiente, applied) else ""
    return ""


def _puede_ingerirse(ruta: Path, applied: BackupId | None) -> bool:
    """La guarda de antigüedad también vale para el fallback de disco.

    `guard_monotonic` corre en `acquire`, pero el fallback no pasa por ahí. Si
    alguien borra el artefacto nuevo y queda uno viejo, ingerirlo retrocedería
    el lago en silencio, que es exactamente lo que esta feature existe para
    impedir. Un nombre que no parsea tampoco se ingiere.
    """
    try:
        candidato = parse_backup_id(ruta.name)
    except BackupNameError:
        return False
    if applied is None:
        return True
    return candidato > applied


def should_mark_applied(outcome: AcquireOutcome | dict | None, load_ok: bool) -> bool:
    """Decide si corresponde `mark_applied`: solo con carga exitosa y backup.

    Pura: nunca se marca cuando la carga falló. Un backup marcado sin
    ingesta se rechaza por antigüedad para siempre y jamás entra al lago.
    """
    if not load_ok or outcome is None:
        return False
    if isinstance(outcome, dict):
        backup_id = outcome.get("backup_id")
    else:
        backup_id = outcome.backup_id
    return backup_id is not None


@task(name="acquire", retries=TASK_RETRIES,
      retry_delay_seconds=TASK_RETRY_DELAY_SECONDS, log_prints=True)
def acquire() -> AcquireOutcome:
    """Etapa 1: trae el `.bak` nuevo desde Gmail a `mssql-backup/`.

    Sin `ACQUIRE_IMAP_USER` o `ACQUIRE_LINK_HOSTS` termina en éxito con
    warning (el workflow manual con `MSSQL_BAK_PATH` sigue intacto). Un
    fallo de adquisición propaga la excepción: la corrida queda en fallido
    en Prefect con el motivo (spec S9).
    """
    logger = get_run_logger()
    imap_user = os.getenv("ACQUIRE_IMAP_USER", "").strip()
    link_hosts = [h.strip() for h in
                  os.getenv("ACQUIRE_LINK_HOSTS", "").split(",")
                  if h.strip()]
    if not imap_user or not link_hosts:
        msg = ("acquire: sin ACQUIRE_IMAP_USER o ACQUIRE_LINK_HOSTS; se salta "
               "la adquisición (bak-extract usa MSSQL_BAK_PATH como siempre)")
        logger.warning(msg)
        print(f"WARNING: {msg}", flush=True)
        return AcquireOutcome(status="no_new_backup",
                              message="skipped: sin config de adquisición")
    outcome = acquire_latest_bak(
        state_path=os.getenv("ACQUIRE_STATE_PATH", ACQUIRE_STATE_DEFAULT),
        incoming_dir=os.getenv("ACQUIRE_INCOMING_DIR", ACQUIRE_INCOMING_DEFAULT),
        final_dir=os.getenv("ACQUIRE_FINAL_DIR", ACQUIRE_FINAL_DEFAULT),
        imap_user=imap_user,
        imap_password=os.getenv("ACQUIRE_IMAP_APP_PASSWORD", ""),
        # Defaults del proveedor en código: el caso normal funciona sin
        # configurar nada, y `tests/test_mailbox.py` los fija contra el correo
        # real. El entorno sigue mandando cuando define la variable.
        sender=os.getenv("ACQUIRE_FROM", "").strip() or FINNEGANS_SENDER,
        subject_pattern=(
            os.getenv("ACQUIRE_SUBJECT_PATTERN", "").strip()
            or FINNEGANS_SUBJECT_PATTERN
        ),
        link_hosts=link_hosts,
        min_window_seconds=int(os.getenv(
            "ACQUIRE_MIN_WINDOW_SECONDS", str(DEFAULT_MIN_WINDOW_SECONDS))),
        # Requisito L10: filtrar por fecha y dejar un solo backup.
        since_days=int(os.getenv("ACQUIRE_SINCE_DAYS", str(DEFAULT_SINCE_DAYS))),
        keep_backups=int(os.getenv("ACQUIRE_KEEP_BACKUPS", str(DEFAULT_KEEP))),
    )
    logger.info("acquire: %s", outcome.message)
    print(f"acquire: {outcome.status}: {outcome.message}", flush=True)
    return outcome


@task(name="bak-extract", retries=TASK_RETRIES,
      retry_delay_seconds=TASK_RETRY_DELAY_SECONDS, log_prints=True)
def bak_extract(bak_path: str | None = None) -> str:
    """Etapa 2: `.bak` nuevo -> Parquet (restaura temporal, extrae, limpia).

    `bak_path` lo pasa el flow (lo adquirido por `acquire`); sin él se usa
    `MSSQL_BAK_PATH` como siempre. Sin variable configurada termina en
    éxito con warning (el operador ya dejó el Parquet). Con `.bak` sin
    cambios respecto a la marca termina en éxito sin tocar el motor. La
    marca se actualiza solo en éxito.
    """
    logger = get_run_logger()
    bak_raw = (bak_path if bak_path is not None
               else os.getenv("MSSQL_BAK_PATH", "")).strip()
    if not bak_raw:
        msg = ("bak-extract: MSSQL_BAK_PATH no definida; se salta la "
               "extracción (bronze-load usa el Parquet ya generado)")
        logger.warning(msg)
        print(f"WARNING: {msg}", flush=True)
        return "skipped: sin MSSQL_BAK_PATH"
    src = Path(bak_raw).expanduser()
    if not src.is_absolute():
        src = (ROOT / src).resolve()
    if not src.is_file():
        raise RuntimeError(
            f"bak-extract: no existe {src} (MSSQL_BAK_PATH mal definida)")
    size1, mtime1 = _stat_sig(src)
    time.sleep(BAK_SETTLE_SECONDS)
    size2, mtime2 = _stat_sig(src)
    if (size1, mtime1) != (size2, mtime2):
        msg = (f"bak-extract: {src.name} cambió durante la espera "
               f"({size1}->{size2} B); posible copia parcial, se salta la extracción")
        logger.warning(msg)
        print(f"WARNING: {msg}", flush=True)
        return "skipped: copia parcial sospechada"
    out = Path(PARQUET_OUT)
    current = {"bak_path": str(src), "size": size2,
               "mtime_ns": mtime2, "out": str(out)}
    mark = _read_mark()
    if mark == current:
        msg = (f"bak-extract: sin .bak nuevo ({src.name}, "
               f"{size2} B); se salta la extracción")
        logger.info(msg)
        print(msg, flush=True)
        return "skipped: sin .bak nuevo"
    # .bak / .tar nuevo (nombre, tamaño, mtime o salida distintos de la marca).
    backup_dir = ROOT / "mssql-backup"
    backup_dir.mkdir(exist_ok=True)
    if _is_tar_archive(src.name):
        with tarfile.open(src, "r:*") as tf:
            bak_members = [m for m in tf.getmembers() if m.isfile() and m.name.lower().endswith(".bak")]
            if not bak_members:
                raise RuntimeError(f"bak-extract: el archivo {src} no contiene ningún .bak")
            member = bak_members[0]
            dest = backup_dir / Path(member.name).name
            msg = f"bak-extract: descomprimiendo {src} ({member.name}) -> {dest}"
            logger.info(msg)
            print(msg, flush=True)
            src_f = tf.extractfile(member)
            if src_f is None:
                raise RuntimeError(f"bak-extract: no se pudo leer {member.name} de {src}")
            with src_f, open(dest, "wb") as out_f:
                shutil.copyfileobj(src_f, out_f)
        os.chmod(dest, 0o644)
    else:
        dest = backup_dir / src.name
        if dest.exists() and os.path.samefile(src, dest):
            msg = f"bak-extract: {src.name} ya está en ./mssql-backup/, sin copia"
            logger.info(msg)
            print(msg, flush=True)
        else:
            shutil.copy2(src, dest)
            os.chmod(dest, 0o644)
            msg = f"bak-extract: copiado {src} -> {dest} ({size2} B)"
            logger.info(msg)
            print(msg, flush=True)
    out.mkdir(parents=True, exist_ok=True)
    cmd = [
        BRONZE_PYTHON, "-m", "bak2parquet", "--engine", "url",
        "--bak", str(dest),
        "--bak-path", f"{MOTOR_BACKUP_DIR}/{dest.name}",
        "--out", str(out),
        "--mssql-host", os.getenv("MSSQL_HOST", "localhost"),
        "--mssql-port", os.getenv("MSSQL_PORT", "1433"),
        "--mssql-user", os.getenv("MSSQL_USER", "sa"),
        "--report-json", str(out / "report.json"),
    ]
    _run(cmd, "bak-extract", extra_env=_extractor_env())
    RESTORE_MARK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESTORE_MARK_PATH, "w", encoding="utf-8") as fh:
        json.dump(current, fh, indent=1)
    logger.info("bak-extract: marca actualizada en %s", RESTORE_MARK_PATH)
    return f"extraído {dest.name} -> {out}"


@task(name="bronze-load", retries=TASK_RETRIES,
      retry_delay_seconds=TASK_RETRY_DELAY_SECONDS, log_prints=True)
def bronze_load() -> str:
    """Etapa 3: Parquet del extractor -> Bronze (reemplazo total por tabla)."""
    out = _run(
        [BRONZE_PYTHON, "bronze/load_parquet.py", "--src", str(PARQUET_OUT)],
        "bronze-load",
    )
    return out


@flow(name=FLOW_NAME, log_prints=True)
def kavos_lake_medallion_flow() -> dict[str, str]:
    """Día completo: adquisición del `.bak`, extracción a Parquet y Bronze
    (fail-stop: si la adquisición o la extracción fallan, no se carga; si la
    carga falla, el backup nunca se marca como aplicado)."""
    outcome = acquire()
    # El estado se lee para que el fallback de disco respete la guarda de
    # antigüedad. Un estado corrupto levanta StateError y falla la corrida:
    # preferimos no ingerir antes que ingerir sin la guarda.
    applied = load_state(
        os.getenv("ACQUIRE_STATE_PATH", ACQUIRE_STATE_DEFAULT)
    ).applied
    extract_log = bak_extract(
        bak_path_for(
            outcome,
            os.getenv("MSSQL_BAK_PATH", ""),
            os.getenv("ACQUIRE_FINAL_DIR", ACQUIRE_FINAL_DEFAULT),
            applied,
        )
    )
    load_log = bronze_load()
    if isinstance(outcome, dict):
        backup_id = outcome.get("backup_id")
    else:
        backup_id = outcome.backup_id
    if should_mark_applied(outcome, True):
        mark_applied(
            os.getenv("ACQUIRE_STATE_PATH", ACQUIRE_STATE_DEFAULT),
            backup_id,
        )
        applied_log = f"applied {backup_id.filename}"
    else:
        applied_log = "skipped: sin backup adquirido"
    if isinstance(outcome, dict):
        acquire_log = f"{outcome.get('status')}: {outcome.get('message', '')}"
    else:
        acquire_log = f"{outcome.status}: {outcome.message}"
    return {"acquire": acquire_log, "bak_extract": extract_log,
            "bronze_load": load_log, "mark_applied": applied_log}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Flow .bak -> Parquet -> Bronze (extracción y carga)")
    ap.parse_args(argv)
    state = kavos_lake_medallion_flow(return_state=True)
    print(f"flow {FLOW_NAME}: {state.name}")
    return 0 if state.is_completed() else 1


if __name__ == "__main__":
    sys.exit(main())
