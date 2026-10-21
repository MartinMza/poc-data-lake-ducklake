"""Dulse Lake Query Engine — Conexión DuckLake, catálogo, KPIs y SQL Guardrails."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import os
import re
import threading
from typing import Any

import duckdb

_lock = threading.Lock()
_con: duckdb.DuckDBPyConnection | None = None


def _env(name: str, default: str) -> str:
    """Obtiene variable de entorno con fallback al archivo .env local si existe."""
    v = os.getenv(name)
    if v is not None:
        return v
    if os.path.exists(".env"):
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, val = line.split("=", 1)
                    if k.strip() == name:
                        return val.strip()
    return default


def get_connection() -> duckdb.DuckDBPyConnection:
    """Retorna la conexión DuckDB singleton con DuckLake y extensiones cargadas."""
    global _con
    with _lock:
        if _con is not None:
            return _con

        cat = (
            f"dbname={_env('CATALOG_DB', 'ducklake_catalog')} "
            f"host={_env('CATALOG_HOST', 'localhost')} "
            f"port={_env('CATALOG_PORT', '5433')} "
            f"user={_env('CATALOG_USER', 'ducklake')} "
            f"password={_env('CATALOG_PASSWORD', 'ducklake')}"
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


FORBIDDEN_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "REPLACE",
    "MERGE",
    "COPY",
    "ATTACH",
    "DETACH",
    "INSTALL",
    "LOAD",
    "PRAGMA",
    "CALL",
    "SET",
    "EXPORT",
    "IMPORT",
    "GRANT",
    "REVOKE",
}

ALLOWED_START_KEYWORDS = ("SELECT", "WITH", "DESCRIBE", "SUMMARIZE", "EXPLAIN")


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

    # Verificar palabras clave prohibidas
    found_forbidden = FORBIDDEN_KEYWORDS.intersection(tokens)
    if found_forbidden:
        raise ValueError(
            f"Operación prohibida: Se detectaron comandos no permitidos de escritura o administración: {', '.join(sorted(found_forbidden))}."
        )

    return clean


def list_tables(schemas: tuple[str, ...] = ("bronze", "silver", "gold")) -> list[dict[str, Any]]:
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


# Curated KPIs
def get_monthly_revenue(currency: str | None = None) -> list[dict[str, Any]]:
    """Retorna métricas de lake.gold.gold_monthly_revenue."""
    sql = "SELECT * FROM lake.gold.gold_monthly_revenue"
    if currency:
        safe_curr = re.sub(r"[^A-Za-z0-9]", "", currency).upper()
        sql += f" WHERE currency = '{safe_curr}'"
    sql += " ORDER BY month DESC"
    res = execute_query(sql, limit=500)
    cols = res["columns"]
    return [dict(zip(cols, r)) for r in res["rows"]]


def get_customer_revenue(limit: int = 20) -> list[dict[str, Any]]:
    """Retorna top clientes según lake.gold.gold_customer_revenue."""
    sql = "SELECT * FROM lake.gold.gold_customer_revenue ORDER BY total_paid DESC"
    res = execute_query(sql, limit=limit)
    cols = res["columns"]
    return [dict(zip(cols, r)) for r in res["rows"]]


def get_unit_performance(limit: int = 100) -> list[dict[str, Any]]:
    """Retorna desempeño de unidades según lake.gold.gold_unit_performance."""
    sql = "SELECT * FROM lake.gold.gold_unit_performance ORDER BY total_collected DESC"
    res = execute_query(sql, limit=limit)
    cols = res["columns"]
    return [dict(zip(cols, r)) for r in res["rows"]]

