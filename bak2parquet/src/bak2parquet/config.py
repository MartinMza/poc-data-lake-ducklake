"""Configuración validada del extractor (T2, S6.3). Fail-closed.

Toda opción llega por CLI o por variable de entorno; lo que no se valida acá
no existe para el resto del paquete. Sin dependencias de terceros para que
`--help` funcione con el Python del sistema.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

# Tamaño de lote por defecto (ejemplo textual de S4: 50.000 filas por batch).
DEFAULT_BATCH_SIZE = 50_000
# Acotan los buffers de pre-asignación del lector ODBC ante VARCHAR(MAX) /
# NVARCHAR(MAX) / VARBINARY(MAX) (S4.4). Sin ellos el driver reserva ~2 GiB
# por columna MAX y rompe la huella plana.
# Techos de preasignación por columna MAX. Chicos a propósito: arrow-odbc
# dimensiona cada lote contra este valor, así que un techo inflado hace creer
# que cada fila mide eso y colapsa los lotes. Medido en la tabla real (56
# columnas, ~140 B/fila): con 4 MiB el lote quedaba en ~11 filas -> 10.910
# lotes -> 29.000 row groups -> 1.235 MB de RSS. Con 64 KiB: 172 lotes y 142 MB.
DEFAULT_MAX_TEXT_SIZE = 65_536  # 64 KiB, en bytes UTF-8.
DEFAULT_MAX_BINARY_SIZE = 65_536  # 64 KiB.
# Techo del buffer de tránsito de arrow-odbc (el default de 512 MiB no entra
# en el presupuesto de memoria del contenedor efímero).
DEFAULT_MAX_BYTES_PER_BATCH = 50_331_648  # 48 MiB (ver _required_overhead_mb).
DEFAULT_ENGINE = "docker"
DEFAULT_IMAGE = "mcr.microsoft.com/mssql/server:2022-CU27-ubuntu-22.04"
# Presupuesto RSS del proceso Python (S1, nota aritmética: 300 MB; 0 lo
# desactiva). NO es `RLIMIT_AS`: limitar el espacio de direcciones mata el
# proceso con SIGSEGV cuando Arrow/Rust reserva memoria virtual aunque el RSS
# sea bajo. La guarda real es el cgroup del contenedor más la medición de RSS
# en proceso (`mem.check_rss_budget`), que aborta con un error limpio.
# Presupuesto de RSS del proceso Python. Medido sobre el backup real (778
# tablas): el piso se estabiliza en ~250 MB y una tabla ancha lo llevó a 311 MB.
# 300 MB era un número de diseño, no una medición. 400 MB deja margen y el
# total sigue dentro del techo de ~2 GB: motor con max server memory=1024 MB
# (RSS medido 1.360 MB) + 400 MB = 1,76 GB. El techo duro real es el cgroup del
# contenedor; este número es la guardia que aborta con error limpio.
DEFAULT_MEMORY_LIMIT_MB = 400

# Presupuesto de memoria del proceso (ver required_memory_mb): el intérprete y
# el writer, más cuántas veces el lote puede estar vivo a la vez durante la
# codificación ZSTD, más un buffer de tránsito por cada columna MAX.
BASE_OVERHEAD_MB = 96
BATCH_COPIES = 3
# Edad mínima para considerar huérfana una base `bak2pq_*` al arrancar (el
# reaper nunca toca una base más joven: una corrida concurrente sigue viva).
DEFAULT_REAP_MAX_AGE_SEC = 3600
DEFAULT_STARTUP_TIMEOUT_SEC = 180

# S1 — límites del motor, congelados por la decisión de ingeniería (ver S1 del
# doc de la tarea: cgroup 1600 MB + MSSQL_MEMORY_LIMIT_MB=1300 + max server
# memory=1024 MB + presupuesto RSS de Python 300 MB).
CONTAINER_MEM_LIMIT = "1600m"
CONTAINER_CPUS = "1.5"
MSSQL_MEMORY_LIMIT_MB = 1300
MAX_SERVER_MEMORY_MB = 1024

ODBC_DRIVER = "ODBC Driver 18 for SQL Server"


class ConfigError(ValueError):
    """Configuración inválida o incompleta: abortar antes de tocar el motor."""


@dataclass(frozen=True)
class Config:
    """Configuración efectiva, ya validada. `mssql_password` nunca se loguea."""

    bak: str
    out: str
    batch_size: int = DEFAULT_BATCH_SIZE
    max_text_size: int = DEFAULT_MAX_TEXT_SIZE
    max_binary_size: int = DEFAULT_MAX_BINARY_SIZE
    max_bytes_per_batch: int = DEFAULT_MAX_BYTES_PER_BATCH
    report_json: str | None = None
    engine: str = DEFAULT_ENGINE
    keep_engine: bool = False
    keep_db: bool = False
    # Ruta del .bak vista por el motor en modo `url` (en modo `docker` se
    # calcula del montaje). None = usar el mismo valor de `bak`.
    bak_path: str | None = None
    mssql_host: str = "localhost"
    mssql_port: int = 1433
    mssql_user: str = "sa"
    mssql_password: str = ""
    image: str = DEFAULT_IMAGE
    memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB
    reap_max_age_sec: int = DEFAULT_REAP_MAX_AGE_SEC
    startup_timeout_sec: int = DEFAULT_STARTUP_TIMEOUT_SEC


_TRUE = frozenset({"1", "true", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "no", "n", "off", ""})


def _env_flag(env: Mapping[str, str], name: str) -> bool | None:
    """Lee un booleano del entorno; None si no está definido."""
    if name not in env:
        return None
    value = env[name].strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name}={env[name]!r}: se esperaba un booleano (0/1/true/false).")


def _env_int(env: Mapping[str, str], name: str) -> int | None:
    """Lee un entero del entorno; None si no está definido."""
    if name not in env:
        return None
    try:
        return int(env[name].strip())
    except ValueError:
        raise ConfigError(f"{name}={env[name]!r}: se esperaba un entero.") from None


def _pick(
    overrides: Mapping[str, Any],
    env: Mapping[str, str],
    field: str,
    env_names: tuple[str, ...],
    default: Any,
) -> Any:
    """Precedencia CLI > entorno > default (T2/S6.3: env o CLI)."""
    if overrides.get(field) is not None:
        return overrides[field]
    for name in env_names:
        if env.get(name) not in (None, ""):
            return env[name]
    return default


def load_config(
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Fusiona CLI (`overrides`) > entorno > defaults y valida fail-closed.

    Lanza `ConfigError` ante cualquier valor ausente o inválido, antes de
    tocar el motor, el disco de salida o la red.
    """
    overrides = dict(overrides or {})
    env = os.environ if env is None else env

    raw_batch = _pick(overrides, env, "batch_size", ("BAK2PARQUET_BATCH_SIZE",), DEFAULT_BATCH_SIZE)
    raw_text = _pick(overrides, env, "max_text_size", ("BAK2PARQUET_MAX_TEXT_SIZE",), DEFAULT_MAX_TEXT_SIZE)
    raw_bin = _pick(overrides, env, "max_binary_size", ("BAK2PARQUET_MAX_BINARY_SIZE",), DEFAULT_MAX_BINARY_SIZE)
    raw_bytes = _pick(overrides, env, "max_bytes_per_batch", ("BAK2PARQUET_MAX_BYTES_PER_BATCH",), DEFAULT_MAX_BYTES_PER_BATCH)
    raw_port = _pick(overrides, env, "mssql_port", ("MSSQL_PORT",), 1433)
    raw_mem = _pick(overrides, env, "memory_limit_mb", ("BAK2PARQUET_MEMORY_LIMIT_MB",), DEFAULT_MEMORY_LIMIT_MB)
    raw_reap = _pick(overrides, env, "reap_max_age_sec", ("BAK2PARQUET_REAP_MAX_AGE_SEC",), DEFAULT_REAP_MAX_AGE_SEC)
    raw_timeout = _pick(overrides, env, "startup_timeout_sec", ("BAK2PARQUET_STARTUP_TIMEOUT_SEC",), DEFAULT_STARTUP_TIMEOUT_SEC)

    try:
        batch_size = int(raw_batch)
        max_text_size = int(raw_text)
        max_binary_size = int(raw_bin)
        max_bytes_per_batch = int(raw_bytes)
        mssql_port = int(raw_port)
        memory_limit_mb = int(raw_mem)
        reap_max_age_sec = int(raw_reap)
        startup_timeout_sec = int(raw_timeout)
    except (TypeError, ValueError):
        raise ConfigError(
            "batch-size, max-text-size, max-binary-size, max-bytes-per-batch, "
            "mssql-port, memory-limit-mb, reap-max-age-sec y startup-timeout-sec "
            "deben ser enteros."
        ) from None

    keep_engine = overrides.get("keep_engine")
    if keep_engine is None:
        keep_engine = _env_flag(env, "BAK2PARQUET_KEEP_ENGINE") or False
    keep_db = overrides.get("keep_db")
    if keep_db is None:
        keep_db = _env_flag(env, "BAK2PARQUET_KEEP_DB") or False

    password = _pick(overrides, env, "mssql_password", ("BAK2PARQUET_MSSQL_PASSWORD", "MSSQL_SA_PASSWORD"), "")

    cfg = Config(
        bak=_pick(overrides, env, "bak", ("BAK2PARQUET_BAK",), ""),
        out=_pick(overrides, env, "out", ("BAK2PARQUET_OUT",), ""),
        batch_size=batch_size,
        max_text_size=max_text_size,
        max_binary_size=max_binary_size,
        max_bytes_per_batch=max_bytes_per_batch,
        report_json=_pick(overrides, env, "report_json", ("BAK2PARQUET_REPORT_JSON",), None),
        engine=str(_pick(overrides, env, "engine", ("BAK2PARQUET_ENGINE",), DEFAULT_ENGINE)).lower(),
        keep_engine=bool(keep_engine),
        keep_db=bool(keep_db),
        bak_path=_pick(overrides, env, "bak_path", ("BAK2PARQUET_BAK_PATH",), None),
        mssql_host=_pick(overrides, env, "mssql_host", ("MSSQL_HOST",), "localhost"),
        mssql_port=mssql_port,
        mssql_user=_pick(overrides, env, "mssql_user", ("MSSQL_USER",), "sa"),
        mssql_password=password,
        image=_pick(overrides, env, "image", ("BAK2PARQUET_IMAGE",), DEFAULT_IMAGE),
        memory_limit_mb=memory_limit_mb,
        reap_max_age_sec=reap_max_age_sec,
        startup_timeout_sec=startup_timeout_sec,
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    """Fail-closed: ningún valor dudoso llega al motor."""
    if not cfg.bak:
        raise ConfigError("falta la ruta del .bak (--bak o BAK2PARQUET_BAK).")
    if not cfg.bak.lower().endswith(".bak"):
        raise ConfigError(f"{cfg.bak!r}: el extractor solo acepta archivos .bak.")
    if not os.path.isfile(cfg.bak):
        raise ConfigError(f"{cfg.bak!r}: no existe o no es un archivo.")
    if not cfg.out:
        raise ConfigError("falta el directorio de salida (--out o BAK2PARQUET_OUT).")
    if cfg.batch_size < 1:
        raise ConfigError(f"batch-size={cfg.batch_size}: debe ser >= 1.")
    for name in ("max_text_size", "max_binary_size", "max_bytes_per_batch"):
        if getattr(cfg, name) < 1:
            raise ConfigError(f"{name}={getattr(cfg, name)}: debe ser >= 1.")
    if cfg.engine not in ("docker", "url"):
        raise ConfigError(f"engine={cfg.engine!r}: se esperaba 'docker' o 'url'.")
    if not (1 <= cfg.mssql_port <= 65535):
        raise ConfigError(f"mssql-port={cfg.mssql_port}: fuera de rango 1-65535.")
    if cfg.engine == "url" and not cfg.mssql_host.strip():
        raise ConfigError("en modo url se necesita --mssql-host (o MSSQL_HOST).")
    if not cfg.mssql_user.strip():
        raise ConfigError("se necesita un usuario de SQL Server (--mssql-user o MSSQL_USER).")
    if not cfg.mssql_password:
        raise ConfigError(
            "se necesita la password de SQL Server (--mssql-password, "
            "BAK2PARQUET_MSSQL_PASSWORD o MSSQL_SA_PASSWORD)."
        )
    if cfg.memory_limit_mb < 0:
        raise ConfigError(f"memory-limit-mb={cfg.memory_limit_mb}: debe ser >= 0 (0 lo desactiva).")
    if cfg.reap_max_age_sec < 0:
        raise ConfigError(f"reap-max-age-sec={cfg.reap_max_age_sec}: debe ser >= 0 (0 desactiva el reaper).")
    # Fail-closed: si el lote no entra en el techo, el proceso NO falla con un
    # error, muere con SIGSEGV u OOM (verificado en vivo). Un límite que no se
    # puede cumplir es peor que no tenerlo: se calcula el techo mínimo y se
    # rechaza la config. El piso supone una columna MAX de texto y una de
    # binario; el chequeo exacto por tabla corre en la corrida (con el conteo
    # real de columnas MAX del catálogo) antes de abrir cada lector.
    if cfg.memory_limit_mb:
        necesario = required_memory_mb(cfg)
        if cfg.memory_limit_mb < necesario:
            raise ConfigError(
                f"memory-limit-mb={cfg.memory_limit_mb} no alcanza para "
                f"max-bytes-per-batch={cfg.max_bytes_per_batch}, "
                f"max-text-size={cfg.max_text_size} y "
                f"max-binary-size={cfg.max_binary_size}: se necesitan ~{necesario} MB "
                "(bajá el lote, o subí el límite, o poné memory-limit-mb=0 para no acotar)."
            )
    if cfg.startup_timeout_sec < 1:
        raise ConfigError(f"startup-timeout-sec={cfg.startup_timeout_sec}: debe ser >= 1.")


def required_memory_mb(cfg: "Config", n_max_text: int = 1, n_max_binary: int = 1) -> int:
    """Techo mínimo de RSS que el lote configurado exige.

    El pico no es el tamaño del lote: ZSTD codifica con buffer de entrada y de
    salida (≈3x el lote), y arrow-odbc preasigna `max_text_size` y
    `max_binary_size` por cada columna de longitud máxima. A eso se suma el
    propio intérprete y el writer de Parquet.

    `n_max_text` / `n_max_binary` es la cantidad de columnas MAX de cada tipo
    en la tabla: el modelo escala con ellas porque cada una preasigna su
    buffer completo (una tabla con 30 VARCHAR(MAX) necesita ~30x más tránsito
    que una con uno solo). En validación de config se usa el piso (1, 1); en
    la corrida se usa el conteo real del catálogo por tabla.
    """
    def mb(bytes_: int) -> int:
        return -(-bytes_ // (1024 * 1024))

    return (
        BASE_OVERHEAD_MB
        + BATCH_COPIES * mb(cfg.max_bytes_per_batch)
        + max(n_max_text, 0) * mb(cfg.max_text_size)
        + max(n_max_binary, 0) * mb(cfg.max_binary_size)
    )


def _escape_conn_value(value: str) -> str:
    """Escapa un valor de connection string ODBC (llaves si tiene `;`)."""
    if any(c in value for c in (";", "{", "}")) or value != value.strip():
        return "{" + value.replace("}", "}}") + "}"
    return value


def odbc_connection_string(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str = "master",
    driver: str = ODBC_DRIVER,
) -> str:
    """Arma el connection string ODBC 18 (cifrado + certificado autofirmado)."""
    return (
        f"Driver={{{driver}}};"
        f"Server={_escape_conn_value(f'{host},{port}')};"
        f"UID={_escape_conn_value(user)};"
        f"PWD={_escape_conn_value(password)};"
        f"Database={_escape_conn_value(database)};"
        "Encrypt=yes;TrustServerCertificate=yes;"
    )


def sanitize_filename(name: str) -> str:
    """Sanea un nombre para usarlo como archivo (contrato: sin `/`, `\\`, `:` ni nulos)."""
    clean = name.strip()
    for bad in ("/", "\\", ":", "\x00"):
        clean = clean.replace(bad, "_")
    return clean or "_"
