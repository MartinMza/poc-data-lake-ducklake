"""Motor SQL Server efímero (T3, S1). Dos modos: `docker` y `url`.

- `docker` (default): levanta un contenedor efímero con cgroup 1600 MB,
  1.5 CPUs y `MSSQL_MEMORY_LIMIT_MB=1300`; al terminar lo elimina.
- `url`: se conecta a una instancia existente; el teardown NUNCA detiene
  el motor, solo elimina la base que esa corrida creó.

En ambos modos se fija `max server memory = 1024 MB` ANTES de cualquier
paso pesado, para que el Buffer Pool nunca crezca sin control.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time

from .config import (
    CONTAINER_CPUS,
    CONTAINER_MEM_LIMIT,
    MAX_SERVER_MEMORY_MB,
    MSSQL_MEMORY_LIMIT_MB,
    Config,
    odbc_connection_string,
)

# Solo los nombres generados por `generate_database_name` pueden borrarse
# (T7: el DROP se niega a cualquier otro nombre).
GENERATED_DB_RE = re.compile(r"^bak2pq_[0-9]+_[0-9]+$")

# Techo textual de S1: la memoria caché interna nunca supera 1200 MB.
MAX_SERVER_MEMORY_CEILING_MB = 1200

CONTAINER_BACKUP_DIR = "/var/opt/mssql/backup"


class EngineError(RuntimeError):
    """Falla del motor (arranque, readiness o `max server memory`)."""


def generate_database_name(*, pid: int | None = None, now_ts: int | None = None) -> str:
    """Nombre de la base temporal. NUNCA deriva del nombre del .bak (T7).

    El nombre del .bak lo elige el operador y puede colisionar con una base
    real; un nombre generado por la corrida es único y seguro de borrar.
    """
    pid = os.getpid() if pid is None else pid
    now_ts = int(time.time()) if now_ts is None else now_ts
    return f"bak2pq_{pid}_{now_ts}"


def max_server_memory_sql(mb: int = MAX_SERVER_MEMORY_MB) -> str:
    """SQL que acota la caché interna del motor (S1.2). Falla si supera 1200."""
    if mb < 1 or mb > MAX_SERVER_MEMORY_CEILING_MB:
        raise ValueError(f"max server memory={mb}: debe estar entre 1 y {MAX_SERVER_MEMORY_CEILING_MB}.")
    # 'max server memory (MB)' es una opción AVANZADA: en un motor recién
    # arrancado sp_configure falla con el error 15123 ("does not exist, or it
    # may be an advanced option") si antes no se habilita 'show advanced
    # options'. Lo detectó la integración contra un SQL Server real, no los
    # tests de strings SQL.
    return (
        "EXEC sp_configure 'show advanced options', 1; RECONFIGURE; "
        f"EXEC sp_configure 'max server memory (MB)', {mb}; RECONFIGURE;"
    )


def connect_control(connection_string: str, *, timeout_sec: int):
    """Conexión de control con pyodbc (solo RESTORE/DBCC/catálogo).

    `autocommit=True` porque RESTORE y RECONFIGURE no corren en transacción.
    Import tardío para que `--help` y los tests sin motor no necesiten pyodbc.
    """
    import pyodbc

    return pyodbc.connect(connection_string, timeout=timeout_sec, autocommit=True)


def apply_max_server_memory(conn, *, mb: int = MAX_SERVER_MEMORY_MB) -> None:
    """Aplica `max server memory` ANTES de cualquier paso pesado (S1.2)."""
    try:
        cursor = conn.cursor()
        # Sentencia por sentencia para que el error señale cuál falló.
        for statement in max_server_memory_sql(mb).split(";"):
            if statement.strip():
                cursor.execute(statement)
    except Exception as exc:
        raise EngineError(f"no se pudo fijar max server memory ({mb} MB): {exc}") from exc


def wait_ready(connect, *, timeout_sec: int, interval_sec: float = 2.0) -> None:
    """Espera al motor con `SELECT 1` hasta el timeout; si no, aborta (S2.3)."""
    deadline = time.monotonic() + timeout_sec
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = connect()
            try:
                conn.cursor().execute("SELECT 1")
            finally:
                conn.close()
            return
        except Exception as exc:  # noqa: BLE001 — se reintenta hasta el timeout.
            last = exc
            time.sleep(interval_sec)
    raise EngineError(f"el motor no respondió en {timeout_sec}s: {last}")


def find_free_port() -> int:
    """Puerto local libre para publicar el SQL Server efímero."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def docker_run_argv(
    *,
    image: str,
    container_name: str,
    password: str,
    host_port: int,
    bak_dir: str,
    memory: str = CONTAINER_MEM_LIMIT,
    cpus: str = CONTAINER_CPUS,
    memory_limit_mb: int = MSSQL_MEMORY_LIMIT_MB,
) -> list[str]:
    """Argv de `docker run` del motor efímero (S1.1: cgroup + CPUs + env)."""
    return [
        "docker", "run", "-d",
        "--name", container_name,
        "--memory", memory,
        "--cpus", cpus,
        "-e", "ACCEPT_EULA=Y",
        "-e", "MSSQL_PID=Developer",
        "-e", f"MSSQL_SA_PASSWORD={password}",
        "-e", f"MSSQL_MEMORY_LIMIT_MB={memory_limit_mb}",
        "-p", f"127.0.0.1:{host_port}:1433",
        "-v", f"{bak_dir}:{CONTAINER_BACKUP_DIR}:ro",
        image,
    ]


class DockerEngine:
    """Motor efímero en contenedor propio (modo `docker`)."""

    def __init__(self, cfg: Config, *, runner=None) -> None:
        self._cfg = cfg
        self._runner = runner or subprocess.run
        self._db_suffix = f"{os.getpid()}_{int(time.time())}"
        self.container_name = f"bak2pq-{self._db_suffix}".replace("_", "-")
        self.host_port: int | None = None
        bak_abs = os.path.abspath(cfg.bak)
        self._bak_dir = os.path.dirname(bak_abs)
        self.bak_path = f"{CONTAINER_BACKUP_DIR}/{os.path.basename(bak_abs)}"

    def connection_string(self, database: str = "master") -> str:
        """Connection string contra el contenedor efímero."""
        if self.host_port is None:
            raise EngineError("el contenedor efímero todavía no se inició (start primero).")
        return odbc_connection_string(
            host="127.0.0.1",
            port=self.host_port,
            user=self._cfg.mssql_user,
            password=self._cfg.mssql_password,
            database=database,
        )

    def start(self) -> None:
        """Levanta el contenedor, espera readiness y acota la caché (S1)."""
        self.host_port = find_free_port()
        argv = docker_run_argv(
            image=self._cfg.image,
            container_name=self.container_name,
            password=self._cfg.mssql_password,
            host_port=self.host_port,
            bak_dir=self._bak_dir,
        )
        try:
            self._runner(argv, check=True, capture_output=True, text=True)
        except Exception as exc:
            raise EngineError(f"no se pudo iniciar el contenedor efímero: {exc}") from exc
        wait_ready(
            lambda: connect_control(self.connection_string("master"), timeout_sec=10),
            timeout_sec=self._cfg.startup_timeout_sec,
        )
        conn = connect_control(self.connection_string("master"), timeout_sec=30)
        try:
            apply_max_server_memory(conn)
        finally:
            conn.close()

    def stop(self) -> None:
        """Elimina el contenedor, salvo `--keep-engine` (debug)."""
        if self._cfg.keep_engine:
            return
        try:
            self._runner(["docker", "rm", "-f", self.container_name], check=False,
                         capture_output=True, text=True)
        except Exception:  # noqa: BLE001 — el teardown no debe romper el exit code.
            pass


class UrlEngine:
    """Conexión a una instancia existente (modo `url`).

    El teardown NUNCA detiene el motor: solo se borra la base de la corrida.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        # En modo url el servidor lee la ruta que ve él, no el cliente: por
        # defecto se usa el mismo valor de --bak, con override --bak-path.
        self.bak_path = cfg.bak_path or cfg.bak

    def connection_string(self, database: str = "master") -> str:
        """Connection string contra la instancia existente."""
        return odbc_connection_string(
            host=self._cfg.mssql_host,
            port=self._cfg.mssql_port,
            user=self._cfg.mssql_user,
            password=self._cfg.mssql_password,
            database=database,
        )

    def start(self) -> None:
        """Readiness + acota la caché ANTES de lo pesado. No crea nada."""
        wait_ready(
            lambda: connect_control(self.connection_string("master"), timeout_sec=10),
            timeout_sec=self._cfg.startup_timeout_sec,
        )
        conn = connect_control(self.connection_string("master"), timeout_sec=30)
        try:
            apply_max_server_memory(conn)
        finally:
            conn.close()

    def stop(self) -> None:
        """No-op a propósito: en modo `url` el teardown no detiene el motor."""
        return None
