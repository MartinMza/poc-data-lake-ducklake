"""Política SQL del sandbox: reglas puras, sin DuckDB ni FastAPI.

Vive en el dominio porque es la regla que decide qué consulta se acepta; el
motor (adaptador de salida) la aplica pero no la define. Así se puede testear
sin abrir una conexión.
"""

from __future__ import annotations

import re

# Esquema único del lago (bronze). silver/gold ya no existen.
DEFAULT_SCHEMAS: tuple[str, ...] = ("bronze",)
# Prefijos de las tablas reales de SQL Server y metadatos internos de dlt.
SQLSERVER_PREFIXES: tuple[str, ...] = ("mssql_",)
DLT_METADATA_PREFIX = "_dlt_"

FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "REPLACE", "MERGE", "COPY", "ATTACH", "DETACH", "INSTALL", "LOAD",
    "PRAGMA", "CALL", "SET", "EXPORT", "IMPORT", "GRANT", "REVOKE",
}

ALLOWED_START_KEYWORDS = ("SELECT", "WITH", "DESCRIBE", "SUMMARIZE", "EXPLAIN")

# Funciones de DuckDB que abren archivos o estado externo. La denylist de
# keywords sólo frena escritura/admin: una query que empieza con SELECT puede
# leer `/app/.env` o `/etc/passwd` con `read_text`/`read_csv`, hacer SSRF con
# `read_csv('http://...')` o enumerar secretos con `duckdb_secrets()`. Nada de
# esto lo necesita una consulta analítica sobre el lago (que referencia
# `lake.bronze.*`), así que se rechaza por nombre de función.
FORBIDDEN_FUNCTIONS = (
    "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json",
    "read_json_auto", "read_json_objects", "read_ndjson", "read_ndjson_auto",
    "read_ndjson_objects", "read_text", "read_blob", "read_xlsx", "read_arrow",
    "glob", "sniff_csv", "parquet_metadata", "parquet_schema",
    "parquet_file_metadata", "parquet_kv_metadata", "parquet_bloom_probe",
    "iceberg_scan", "iceberg_metadata", "iceberg_snapshots", "delta_scan",
    "st_read", "st_readosm", "duckdb_secrets", "which_secret",
    "load_aws_credentials",
)
_FORBIDDEN_FUNCTION_RE = re.compile(
    r"\b(?:" + "|".join(FORBIDDEN_FUNCTIONS) + r")\s*\(", re.IGNORECASE
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


def validate_sql(sql: str) -> str:
    """Valida que una consulta sea estrictamente de solo lectura."""
    clean = sql.strip()
    if not clean:
        raise ValueError("La consulta SQL no puede estar vacía.")

    # Remover punto y coma final si existe
    if clean.endswith(";"):
        clean = clean[:-1].strip()

    # Bloquear sentencias múltiples
    if ";" in clean:
        raise ValueError("No se permiten múltiples sentencias SQL en una sola petición.")

    tokens = [t.upper() for t in re.findall(r"\b[A-Za-z_]+\b", clean)]
    if not tokens:
        raise ValueError("Consulta SQL inválida.")

    first_word = tokens[0]
    if first_word not in ALLOWED_START_KEYWORDS:
        raise ValueError(
            f"Sentencia no permitida: debe comenzar con {'/'.join(ALLOWED_START_KEYWORDS)}. Recibido: '{first_word}'."
        )

    found_forbidden = FORBIDDEN_KEYWORDS.intersection(tokens)
    if found_forbidden:
        raise ValueError(
            f"Operación prohibida: Se detectaron comandos no permitidos de escritura o administración: {', '.join(sorted(found_forbidden))}."
        )

    reader = _FORBIDDEN_FUNCTION_RE.search(clean)
    if reader is not None:
        raise ValueError(
            "Operación prohibida: no se permite leer archivos ni estado externo "
            f"(función '{reader.group(0)[:-1].strip()}')."
        )

    # Bloquear URLs/rutas externas en literales: cierra SSRF (`http(s)://`) y el
    # acceso cruzado a otros buckets (`s3://`).
    if "://" in clean:
        raise ValueError(
            "Operación prohibida: no se permiten URLs ni rutas externas (esquema '://')."
        )

    return clean


def validate_identifier(name: str, *, kind: str) -> str:
    """Valida un identificador SQL (letras, números y guión bajo)."""
    if not name or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"{kind} inválido: {name!r}")
    return name


def is_sqlserver_table(table: str) -> bool:
    """True si la tabla de bronze viene de SQL Server (y no es metadato dlt)."""
    return table.startswith(SQLSERVER_PREFIXES) and not table.startswith(
        DLT_METADATA_PREFIX
    )
