"""Higiene de memoria y limpieza final (T7, S5.1-S5.2).

- `DBCC DROPCLEANBUFFERS` tras cada tabla: la siguiente empieza con memoria libre.
- `DROP DATABASE` con guarda: SOLO borra nombres generados por esta misma
  corrida (`bak2pq_<pid>_<ts>` en el set `owned`). Es la única operación
  destructiva del paquete y se niega a cualquier otro nombre.
"""

from __future__ import annotations

from datetime import datetime
from typing import Container

from .engine import GENERATED_DB_RE
from .restore import database_state, quote_ident, quote_literal


class RefusedDropError(RuntimeError):
    """El DROP se negó: el nombre no lo creó esta corrida (T7, seguridad)."""


def dropcleanbuffers_sql() -> str:
    """Purge de páginas limpias del Buffer Pool entre tablas (S5.1)."""
    return "DBCC DROPCLEANBUFFERS;"


def is_owned_db_name(name: str, owned: Container[str]) -> bool:
    """True solo si el nombre lo generó esta corrida y está en su set."""
    return bool(name) and name in owned and GENERATED_DB_RE.fullmatch(name) is not None


def drop_database_sql(name: str) -> str:
    """DROP con SINGLE_USER para soltar conexiones residuales antes de borrar."""
    return (
        f"IF DB_ID({quote_literal(name)}) IS NOT NULL BEGIN "
        f"ALTER DATABASE {quote_ident(name)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
        f"DROP DATABASE {quote_ident(name)}; END;"
    )


def drop_database_direct_sql(name: str) -> str:
    """DROP sin ALTER previo: para bases que no están ONLINE.

    El `ALTER DATABASE ... SET SINGLE_USER` está prohibido mientras la base
    restaura (error 5052) y aborta dejando basura; en cambio el `DROP DATABASE`
    directo sí funciona sobre una base en RESTORING (probado en vivo).
    """
    return (
        f"IF DB_ID({quote_literal(name)}) IS NOT NULL "
        f"DROP DATABASE {quote_ident(name)};"
    )


def purge_buffers(conn) -> None:
    """Ejecuta DROPCLEANBUFFERS (control, pyodbc). No falla la corrida si falla."""
    conn.cursor().execute(dropcleanbuffers_sql())


def drop_database(conn, name: str, *, owned: Container[str]) -> None:
    """Elimina la base temporal; se niega a borrar lo que no creó (S5.2).

    Si la base no está ONLINE (RESTORING tras un restore fallido, o estado
    ilegible) se hace DROP directo: el ALTER previo fallaría con 5052 y la
    corrida dejaría una `bak2pq_*` huérfana. Ningún camino deja la base atrás.
    """
    if not is_owned_db_name(name, owned):
        raise RefusedDropError(
            f"DROP DATABASE denegado para {name!r}: no es una base creada por esta corrida."
        )
    state = database_state(conn, name)
    if state == "ONLINE":
        conn.cursor().execute(drop_database_sql(name))
    else:
        conn.cursor().execute(drop_database_direct_sql(name))


# El reaper solo mira nombres generados (`bak2pq_<pid>_<ts>`). `LIKE` necesita
# escapar el guion bajo (si no, `bak2pqX...` también matchearía).
STALE_DATABASES_SQL = (
    "SELECT name, create_date FROM sys.databases "
    "WHERE name LIKE 'bak2pq\\_%' ESCAPE '\\';"
)


def reap_stale_databases(conn, *, max_age_sec: int, now: datetime | None = None) -> list[str]:
    """Elimina bases `bak2pq_*` huérfanas de corridas muertas (crash-safe).

    Un SIGSEGV/OOM salta el `finally`: la base restaurada (GBs de `.mdf`/
    `.ldf`) sobrevive. Al arrancar, antes de restaurar, se barren los
    candidatos y se elimina cada uno que cumpla TODO:

    1. el nombre matchea `GENERATED_DB_RE` (solo lo generado por el extractor);
    2. `create_date` es más vieja que `max_age_sec` (una base fresca puede ser
       de una corrida concurrente: nunca se toca).

    `max_age_sec <= 0` desactiva el reaper. Devuelve los nombres eliminados
    (para loguearlos); los errores por base se tragan para no abortar el
    arranque: lo peor es dejar basura, nunca romper la corrida.
    """
    if max_age_sec <= 0:
        return []
    moment = now if now is not None else datetime.now()
    try:
        cur = conn.cursor()
        cur.execute(STALE_DATABASES_SQL)
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001 — sin catálogo no hay reaper.
        return []
    reaped: list[str] = []
    for row in rows:
        if isinstance(row, dict):
            name, created = row.get("name"), row.get("create_date")
        else:
            seq = tuple(row)
            if len(seq) < 2:
                continue
            name, created = seq[0], seq[1]
        if not isinstance(name, str) or GENERATED_DB_RE.fullmatch(name) is None:
            continue
        try:
            age_sec = (moment - created).total_seconds()  # type: ignore[operator]
        except Exception:  # noqa: BLE001 — fecha ilegible: no se toca.
            continue
        if age_sec <= max_age_sec:
            continue
        try:
            if database_state(conn, name) == "ONLINE":
                conn.cursor().execute(drop_database_sql(name))
            else:
                conn.cursor().execute(drop_database_direct_sql(name))
        except Exception:  # noqa: BLE001 — se sigue con la siguiente.
            continue
        reaped.append(name)
    return reaped
