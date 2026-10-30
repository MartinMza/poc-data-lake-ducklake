"""KavosLake Query Engine — conexión DuckLake, catálogo, tablas MSSQL y guardrails SQL.

El lago contiene únicamente las 7 tablas reales ingestadas desde SQL Server
(`bronze.mssql_bs_*` y `bronze.mssql_d_mart_*`) más los metadatos internos de
dlt. No hay capas dbt (silver/gold) ni origen sintético.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import re
import threading
from typing import Any

import duckdb

from kavos_lake.domain.sql_policy import (
    ALLOWED_START_KEYWORDS,
    DEFAULT_SCHEMAS,
    DLT_METADATA_PREFIX,
    FORBIDDEN_FUNCTIONS,
    FORBIDDEN_KEYWORDS,
    SQLSERVER_PREFIXES,
    is_sqlserver_table,
    validate_identifier as _validate_identifier,
    validate_sql,
)

from kavos_lake.infrastructure.config import assert_safe_credentials, env as _env

_lock = threading.Lock()
_con: duckdb.DuckDBPyConnection | None = None

def get_connection() -> duckdb.DuckDBPyConnection:
    """Retorna la conexión DuckDB singleton con DuckLake y extensiones cargadas."""
    global _con
    with _lock:
        if _con is not None:
            return _con

        catalog_host = _env("CATALOG_HOST", "localhost")
        catalog_password = _env("CATALOG_PASSWORD", "ducklake")
        assert_safe_credentials(
            host=catalog_host, secret=catalog_password, label="Catálogo DuckLake"
        )
        cat = (
            f"dbname={_env('CATALOG_DB', 'ducklake_catalog')} "
            f"host={catalog_host} "
            f"port={_env('CATALOG_PORT', '5433')} "
            f"user={_env('CATALOG_USER', 'ducklake')} "
            f"password={catalog_password}"
        )
        garage_host = _env("GARAGE_HOST", "localhost")
        garage_port = _env("GARAGE_S3_PORT", "3900")
        endpoint = _env("GARAGE_S3_ENDPOINT", f"{garage_host}:{garage_port}")
        endpoint_host = endpoint.removeprefix("http://").removeprefix("https://")

        con = duckdb.connect()
        con.execute("INSTALL ducklake; LOAD ducklake;")
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(
            "CREATE OR REPLACE SECRET garage_s3 (TYPE S3, "
            f"KEY_ID '{_env('GARAGE_S3_KEY_ID', 'GK0123456789abcdef0123456789abcdef')}', "
            f"SECRET '{_env('GARAGE_S3_SECRET', '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef')}', "
            f"ENDPOINT '{endpoint_host}', URL_STYLE 'path', USE_SSL false, "
            f"REGION '{_env('GARAGE_S3_REGION', 'garage')}');"
        )
        data_path = _env("DUCKLAKE_DATA_PATH", "s3://lakehouse/data/")
        try:
            con.execute(
                f"ATTACH 'ducklake:postgres:{cat}' AS lake (DATA_PATH '{data_path}')"
            )
        except Exception as exc:  # noqa: BLE001
            if "does not match existing data path" in str(exc):
                con.execute(f"ATTACH 'ducklake:postgres:{cat}' AS lake")
            else:
                raise

        # Endurecimiento del sandbox (defensa en profundidad, junto a
        # validate_sql). Debe ir DESPUÉS del ATTACH y del CREATE SECRET: el
        # acceso al catálogo (postgres) y a los datos (S3) ya quedó montado.
        #
        # - disabled_filesystems='LocalFileSystem': corta toda lectura del
        #   filesystem local (functions como read_text/read_csv y replacement
        #   scans tipo FROM '/etc/passwd.parquet'). S3FileSystem queda activo,
        #   así que las lecturas del lago siguen andando. Contrapartida
        #   aceptada: el spill a disco local queda deshabilitado; para el subset
        #   del PoC las queries entran en memoria y, si no, fallan con un error
        #   claro en vez de exponer el filesystem.
        # - lock_configuration=true: impide revertir lo anterior desde una
        #   query (además SET ya está en FORBIDDEN_KEYWORDS). Va al final.
        con.execute("SET disabled_filesystems='LocalFileSystem'")
        con.execute("SET lock_configuration=true")
        _con = con
        return _con


def serialize_value(val: Any) -> Any:
    """Convierte tipos no serializables nativamente (Decimal, datetime, bytes) a JSON estándar."""
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, bytes):
        return val.hex()
    if isinstance(val, dict):
        return {k: serialize_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [serialize_value(v) for v in val]
    return val


def list_tables(schemas: tuple[str, ...] = DEFAULT_SCHEMAS) -> list[dict[str, Any]]:
    """Lista todas las tablas en los esquemas dados junto con su conteo de filas."""
    con = get_connection()
    results: list[dict[str, Any]] = []
    with _lock:
        cur = con.cursor()
        for schema in schemas:
            rows = cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = ? ORDER BY table_name",
                (schema,),
            ).fetchall()
            for (t_name,) in rows:
                try:
                    count = cur.execute(
                        f'SELECT COUNT(*) FROM lake."{schema}"."{t_name}"'
                    ).fetchone()[0]
                except Exception:
                    count = -1
                results.append(
                    {
                        "schema": schema,
                        "table": t_name,
                        "full_name": f"{schema}.{t_name}",
                        "row_count": count,
                    }
                )
    return results


def describe_table(schema: str, table: str) -> dict[str, Any]:
    """Retorna la lista de columnas, tipos, conteo y 5 filas de muestra de una tabla."""
    # Validación de identificadores (solo letras, números y guiones bajos)
    if not re.match(r"^[A-Za-z0-9_]+$", schema) or not re.match(r"^[A-Za-z0-9_]+$", table):
        raise ValueError(f"Identificador inválido: {schema}.{table}")

    con = get_connection()
    with _lock:
        cur = con.cursor()
        # Verificar existencia
        exists = cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            (schema, table),
        ).fetchone()
        if not exists:
            raise KeyError(f"La tabla '{schema}.{table}' no existe en el catálogo.")

        # Obtener columnas
        cols_info = cur.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            (schema, table),
        ).fetchall()
        columns = [
            {"name": c[0], "type": c[1], "nullable": c[2] == "YES"} for c in cols_info
        ]

        # Conteo
        total_rows = cur.execute(
            f'SELECT COUNT(*) FROM lake."{schema}"."{table}"'
        ).fetchone()[0]

        # Muestra (5 filas)
        col_names = [c["name"] for c in columns]
        sample_rows = cur.execute(
            f'SELECT * FROM lake."{schema}"."{table}" LIMIT 5'
        ).fetchall()

        sample_dicts = [
            {col: serialize_value(val) for col, val in zip(col_names, row)}
            for row in sample_rows
        ]

    return {
        "schema": schema,
        "table": table,
        "row_count": total_rows,
        "columns": columns,
        "sample": sample_dicts,
    }


def list_snapshots(limit: int = 10) -> list[dict[str, Any]]:
    """Lista los snapshots históricos registrados en DuckLake para Time Travel."""
    con = get_connection()
    limit = max(1, min(limit, 100))
    with _lock:
        cur = con.cursor()
        rows = cur.execute(
            f"SELECT snapshot_id, snapshot_time, schema_version, changes, author, commit_message "
            f"FROM ducklake_snapshots('lake') ORDER BY snapshot_id DESC LIMIT {limit}"
        ).fetchall()
        snapshots = [
            {
                "snapshot_id": r[0],
                "snapshot_time": serialize_value(r[1]),
                "schema_version": r[2],
                "changes": serialize_value(r[3]),
                "author": r[4],
                "commit_message": r[5],
            }
            for r in rows
        ]
    return snapshots


def execute_query(sql: str, limit: int = 100) -> dict[str, Any]:
    """Ejecuta una consulta SQL validada en modo solo lectura con limitación de filas."""
    validated_sql = validate_sql(sql)
    safe_limit = max(1, min(limit, 1000))

    # Envolver la consulta con LIMIT si es un SELECT o WITH
    if validated_sql.upper().startswith(("SELECT", "WITH")):
        exec_sql = f"SELECT * FROM ({validated_sql}) AS _lake_subquery LIMIT {safe_limit}"
    else:
        exec_sql = validated_sql

    con = get_connection()
    with _lock:
        cur = con.cursor()
        cur.execute(exec_sql)
        description = cur.description or []
        col_names = [col[0] for col in description]
        raw_rows = cur.fetchall()

    serialized_rows = [
        [serialize_value(val) for val in row] for row in raw_rows
    ]

    return {
        "columns": col_names,
        "rows": serialized_rows,
        "row_count": len(serialized_rows),
        "limit_applied": safe_limit,
        "sql_executed": exec_sql,
    }


def sqlserver_tables(schema: str = "bronze") -> list[dict[str, Any]]:
    """Inventario de las tablas cargadas desde SQL Server.

    Devuelve, por tabla: conteo de filas, cantidad de columnas, nombres de
    columnas y el último `_dlt_load_id` (marca de la corrida de ingesta que
    escribió la fila más reciente). Excluye los metadatos internos de dlt.
    """
    schema = _validate_identifier(schema, kind="Esquema")
    con = get_connection()
    inventory: list[dict[str, Any]] = []
    with _lock:
        cur = con.cursor()
        names = [
            row[0]
            for row in cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = ? ORDER BY table_name",
                (schema,),
            ).fetchall()
        ]
        for name in names:
            if not is_sqlserver_table(name):
                continue
            columns = cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? "
                "ORDER BY ordinal_position",
                (schema, name),
            ).fetchall()
            column_names = [col[0] for col in columns]
            row_count = cur.execute(
                f'SELECT COUNT(*) FROM lake."{schema}"."{name}"'
            ).fetchone()[0]
            last_load_id = None
            if "_dlt_load_id" in column_names:
                last_load_id = cur.execute(
                    f'SELECT MAX(_dlt_load_id) FROM lake."{schema}"."{name}"'
                ).fetchone()[0]
            inventory.append(
                {
                    "schema": schema,
                    "table": name,
                    "row_count": row_count,
                    "column_count": len(column_names),
                    "last_load_id": serialize_value(last_load_id),
                    "columns": column_names,
                }
            )
    return inventory


def preview_rows(
    schema: str,
    table: str,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Devuelve una página de filas de una tabla del lago (máx. 100 por página)."""
    schema = _validate_identifier(schema, kind="Esquema")
    table = _validate_identifier(table, kind="Tabla")
    safe_limit = max(1, min(limit, 100))
    safe_offset = max(0, offset)

    con = get_connection()
    with _lock:
        cur = con.cursor()
        columns = cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            (schema, table),
        ).fetchall()
        if not columns:
            raise KeyError(f"La tabla '{schema}.{table}' no existe en el catálogo.")
        column_names = [col[0] for col in columns]
        total_rows = cur.execute(
            f'SELECT COUNT(*) FROM lake."{schema}"."{table}"'
        ).fetchone()[0]
        rows = cur.execute(
            f'SELECT * FROM lake."{schema}"."{table}" '
            f"LIMIT {safe_limit} OFFSET {safe_offset}"
        ).fetchall()

    return {
        "schema": schema,
        "table": table,
        "total_rows": total_rows,
        "limit": safe_limit,
        "offset": safe_offset,
        "row_count": len(rows),
        "columns": column_names,
        "rows": [[serialize_value(value) for value in row] for row in rows],
    }
