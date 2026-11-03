"""Restauración autónoma del `.bak` (T4, S2).

Descubre los archivos lógicos con `RESTORE FILELISTONLY`, los reubica con
`WITH MOVE` a las rutas del contenedor y aborta con el motivo exacto del
motor si algo falla. Solo genera SQL y usa conexiones de control (pyodbc):
nunca toca filas de usuario.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Sequence


class RestoreError(RuntimeError):
    """La restauración falló: lleva el motivo exacto del motor (S2.3)."""


def quote_literal(value: str) -> str:
    """Literal N'...' con escape de comillas simples."""
    return "N'" + value.replace("'", "''") + "'"


def quote_ident(name: str) -> str:
    """Identificador [..] con escape de corchetes."""
    return "[" + name.replace("]", "]]") + "]"


def filelistonly_sql(container_bak: str) -> str:
    """Inspección previa de metadatos del .bak (S2.1)."""
    return f"RESTORE FILELISTONLY FROM DISK = {quote_literal(container_bak)};"


def server_paths_sql() -> str:
    """Rutas de datos/log por defecto de la instancia (destino del MOVE)."""
    return (
        "SELECT "
        "SERVERPROPERTY('InstanceDefaultDataPath') AS data_path, "
        "SERVERPROPERTY('InstanceDefaultLogPath') AS log_path;"
    )


@dataclass(frozen=True)
class FileEntry:
    """Archivo lógico del backup: datos (D) o log (L)."""

    logical: str
    kind: str  # "D" = .mdf/.ndf, "L" = .ldf.


def parse_filelist_rows(rows: Iterable) -> list[FileEntry]:
    """Normaliza filas de FILELISTONLY (mappings o tuplas) a `FileEntry`.

    Acepta dicts (`LogicalName`/`Type`) o secuencias (LogicalName, ..., Type
    en las posiciones 0 y 2 del resultado de FILELISTONLY). Ignora cualquier
    tipo que no sea D/L, igual que el `case *) continue` del init actual.
    """
    entries: list[FileEntry] = []
    for row in rows:
        if isinstance(row, dict):
            logical, kind = row.get("LogicalName"), row.get("Type")
        elif hasattr(row, "keys"):
            try:
                mapping = dict(zip(row.keys(), tuple(row)))
            except Exception:  # noqa: BLE001 — fila ilegible: se ignora.
                continue
            logical, kind = mapping.get("LogicalName"), mapping.get("Type")
        else:
            seq = tuple(row)
            if len(seq) < 3:
                continue
            logical, kind = seq[0], seq[2]
        if logical is None or kind is None:
            continue
        kind = str(kind).strip().upper()
        if kind not in ("D", "L"):
            continue
        entries.append(FileEntry(logical=str(logical).strip(), kind=kind))
    return entries


def sanitize_file_stem(logical: str) -> str:
    """Sanea el nombre lógico para el archivo físico (igual que el init: `tr -c 'A-Za-z0-9_-'`)."""
    return "".join(c if (c.isascii() and (c.isalnum() or c in ("_", "-"))) else "_" for c in logical.strip())


def _join_dir(path: str) -> str:
    """Asegura el separador final del directorio destino."""
    if path.endswith(("\\", "/")):
        return path
    return path + "/"


def build_move_list(
    entries: Sequence[FileEntry],
    *,
    db_name: str,
    data_dir: str,
    log_dir: str,
) -> list[str]:
    """Cláusulas MOVE: primer D → .mdf, siguientes D → .ndf, L → .ldf (S2.2)."""
    moves: list[str] = []
    data_n = 0
    for entry in entries:
        stem = sanitize_file_stem(entry.logical)
        if entry.kind == "D":
            data_n += 1
            ext = "mdf" if data_n == 1 else "ndf"
            dest = f"{_join_dir(data_dir)}{db_name}_{stem}.{ext}"
        else:
            dest = f"{_join_dir(log_dir)}{db_name}_{stem}.ldf"
        moves.append(f"MOVE {quote_literal(entry.logical)} TO {quote_literal(dest)}")
    return moves


def restore_sql(db_name: str, container_bak: str, moves: Sequence[str]) -> str:
    """RESTORE DATABASE con reubicación dinámica (S2.2)."""
    with_clause = ", ".join(moves + ["REPLACE", "RECOVERY"])
    return (
        f"RESTORE DATABASE {quote_ident(db_name)} "
        f"FROM DISK = {quote_literal(container_bak)} WITH {with_clause};"
    )


def database_state_sql(db_name: str) -> str:
    """Estado de una base por nombre (`sys.databases.state_desc`)."""
    return f"SELECT state_desc FROM sys.databases WHERE name = {quote_literal(db_name)};"


def drain_results(cursor) -> int:
    """Consume todos los juegos de resultados pendientes del cursor.

    pyodbc devuelve el control de `execute()` tras el primer DONE del lote,
    pero el `RESTORE ... WITH RECOVERY` emite su avance y la fase de recovery
    como juegos de resultados adicionales. Si el cliente cierra la conexión o
    lanza otro lote sin consumirlos con `nextset()`, el motor abandona lo que
    falta y la base queda en RESTORING para siempre (sqlcmd no sufre esto
    porque drena todo). NO es un problema de transacciones: con
    `autocommit=True` y `@@TRANCOUNT = 0` se reproduce igual, y el prefijo
    `SET IMPLICIT_TRANSACTIONS OFF` tampoco lo evita (probado en vivo).
    """
    drained = 0
    while cursor.nextset():
        drained += 1
    return drained


def database_state(conn, db_name: str) -> str | None:
    """Lee `state_desc` de la base; None si no existe o no se puede leer."""
    try:
        cur = conn.cursor()
        cur.execute(database_state_sql(db_name))
        row = cur.fetchone()
    except Exception:
        return None
    if row is None:
        return None
    if isinstance(row, dict):
        value = row.get("state_desc")
    elif hasattr(row, "keys"):
        try:
            value = dict(zip(row.keys(), tuple(row))).get("state_desc")
        except Exception:  # noqa: BLE001 — fila ilegible: se trata como ausente.
            return None
    else:
        seq = tuple(row)
        value = seq[0] if seq else None
    if value is None:
        return None
    return str(value).strip().upper()


def wait_online(conn, db_name: str, *, timeout_sec: float = 60.0,
                interval_sec: float = 1.0) -> None:
    """Exige que la base llegue a ONLINE; si no, aborta con el estado (S2.3)."""
    deadline = time.monotonic() + timeout_sec
    last: str | None = None
    while True:
        try:
            last = database_state(conn, db_name)
        except Exception as exc:  # noqa: BLE001 — se reintenta hasta el timeout.
            last = f"ilegible ({exc})"
        if last == "ONLINE":
            return
        if time.monotonic() >= deadline:
            raise RestoreError(
                f"la base [{db_name}] no llegó a ONLINE "
                f"(último estado: {last})."
            )
        time.sleep(interval_sec)


def restore_database(conn, *, db_name: str, container_bak: str,
                     wait_online_sec: float = 60.0) -> list[str]:
    """Ejecuta FILELISTONLY → MOVE → RESTORE y exige ONLINE.

    Devuelve los MOVE aplicados. Cualquier falla (corrupto, versión
    incompatible o falta de espacio) aborta con `RestoreError` que conserva
    el motivo exacto del motor (S2.3); también aborta si la base no llega a
    ONLINE tras drenar el RESTORE.
    """
    try:
        cur = conn.cursor()
        cur.execute(filelistonly_sql(container_bak))
        entries = parse_filelist_rows(cur.fetchall())
        cur.execute(server_paths_sql())
        paths = tuple(cur.fetchone())
        data_dir, log_dir = str(paths[0]), str(paths[1])
    except Exception as exc:
        raise RestoreError(f"no se pudo inspeccionar el .bak ({container_bak}): {exc}") from exc
    if not any(e.kind == "D" for e in entries):
        raise RestoreError(f"el .bak no declara archivos de datos (.mdf/.ndf): {container_bak}")
    if not data_dir or not log_dir:
        raise RestoreError("el motor no informó sus rutas de datos/log (SERVERPROPERTY vacío).")
    moves = build_move_list(entries, db_name=db_name, data_dir=data_dir, log_dir=log_dir)
    try:
        restore_cur = conn.cursor()
        restore_cur.execute(restore_sql(db_name, container_bak, moves))
        drain_results(restore_cur)
    except Exception as exc:
        raise RestoreError(f"RESTORE DATABASE [{db_name}] falló: {exc}") from exc
    wait_online(conn, db_name, timeout_sec=wait_online_sec)
    return moves
