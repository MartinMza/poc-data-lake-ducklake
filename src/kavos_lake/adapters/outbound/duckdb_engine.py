"""KavosLake Query Engine — conexión DuckLake, catálogo, tablas MSSQL y guardrails SQL.

El lago contiene únicamente la data ingestada desde SQL Server: el catálogo
completo de la base en el esquema `bronze` (tablas `mssql_*`). No hay capas
dbt (silver/gold) ni origen sintético. Los conteos cambian con cada backup:
se consultan, no se citan de memoria.
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

from kavos_lake.infrastructure import config

_lock = threading.Lock()
_con: duckdb.DuckDBPyConnection | None = None

def get_connection() -> duckdb.DuckDBPyConnection:
    """Retorna la conexión DuckDB singleton con DuckLake y extensiones cargadas."""
    global _con
    with _lock:
        if _con is not None:
            return _con

        settings = config.get_settings()
        config.assert_safe_credentials(
            host=settings.catalog_host,
            secret=settings.catalog_password.get_secret_value(),
            label="Catálogo DuckLake",
        )
        cat = settings.catalog_dsn
        # Endpoint derivado de host + puerto (no hay variable combinada) y SSL
        # derivado del esquema: http -> false, https -> true.
        endpoint_host = settings.garage_s3_endpoint

        con = duckdb.connect()
        con.execute("INSTALL ducklake; LOAD ducklake;")
        con.execute("INSTALL postgres; LOAD postgres;")
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(
            "CREATE OR REPLACE SECRET garage_s3 (TYPE S3, "
            f"KEY_ID '{settings.garage_s3_key_id}', "
            f"SECRET '{settings.garage_s3_secret.get_secret_value()}', "
            f"ENDPOINT '{endpoint_host}', URL_STYLE 'path', "
            f"USE_SSL {'true' if settings.garage_use_ssl else 'false'}, "
            f"REGION '{settings.garage_s3_region}');"
        )
        data_path = settings.ducklake_data_path
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


def _ultimas_escrituras(cur) -> dict[str, tuple[Any, Any]]:
    """Snapshot y hora de la última escritura de cada tabla del lago.

    DuckLake registra un snapshot por commit y, en `changes.tables_created`,
    qué tablas se crearon en él. La ingesta reemplaza la tabla completa
    (drop + create), así que el último snapshot que la creó es su última
    escritura.

    Devuelve `{"bronze.<tabla>": (snapshot_id, snapshot_time)}`.
    """
    tiempos = dict(
        cur.execute("SELECT snapshot_id, snapshot_time FROM ducklake_snapshots('lake')").fetchall()
    )
    filas = cur.execute(
        """
        WITH escrituras AS (
            SELECT unnest(changes.tables_created) AS calificada, snapshot_id
            FROM ducklake_snapshots('lake')
            WHERE changes.tables_created IS NOT NULL
        )
        SELECT calificada, max(snapshot_id) FROM escrituras GROUP BY 1
        """
    ).fetchall()
    return {calificada: (sid, tiempos.get(sid)) for calificada, sid in filas}


def sqlserver_tables(schema: str = "bronze") -> list[dict[str, Any]]:
    """Inventario de las tablas cargadas desde SQL Server.

    Devuelve, por tabla: conteo de filas, cantidad de columnas, nombres de
    columnas y la frescura de la última escritura, tomada de los snapshots de
    DuckLake: `last_load_id` (snapshot) y `last_load_at` (su hora).

    Antes la frescura se leía de `_dlt_load_id`, una columna que sólo escribía
    dlt y que el loader actual no escribe. Devolvía `None` en las 788 tablas sin
    ningún error, así que el dato de frescura no existía y nada lo delataba.
    """
    schema = _validate_identifier(schema, kind="Esquema")
    con = get_connection()
    inventory: list[dict[str, Any]] = []
    with _lock:
        cur = con.cursor()
        ultimas = _ultimas_escrituras(cur)
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
            last_load_id, last_load_at = ultimas.get(f"{schema}.{name}", (None, None))
            inventory.append(
                {
                    "schema": schema,
                    "table": name,
                    "row_count": row_count,
                    "column_count": len(column_names),
                    "last_load_id": last_load_id,
                    "last_load_at": serialize_value(last_load_at),
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
