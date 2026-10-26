#!/usr/bin/env python3
"""Purga del DuckLake: deja SOLO las 7 tablas reales cargadas desde SQL Server.

Contexto: el PoC arrancó con un origen Postgres sintético (`customers/units/
leases/payments`), un esquema MSSQL de prueba (`mssql_productos/ventas/
movimientos`), fixtures de dbt y las capas dbt `silver`/`gold`. Decisión del
operador: el lago queda únicamente con lo que se ingesta desde SQL Server.

Qué conserva:
- `bronze.mssql_bs_*` y `bronze.mssql_d_mart_*` (las 7 tablas reales)
- metadatos internos de dlt (`bronze._dlt_loads`, `_dlt_pipeline_state`,
  `_dlt_version`): estado del pipeline, no data de ejemplo

Qué borra (idempotente, solo lectura en dry-run):
- cualquier otra tabla de `bronze` (sintéticas y de prueba MSSQL)
- los esquemas `bronze_fixture` (fixtures dbt), `bronze_staging` y
  `staging_mssql` (staging transitorio), `silver` y `gold` (capas dbt)

Uso (venv con duckdb fijado, p. ej. bronze/.venv):
    bronze/.venv/bin/python scripts/purge_sample_data.py            # dry-run
    bronze/.venv/bin/python scripts/purge_sample_data.py --apply    # borra

El script nunca toca las 7 tablas reales: si el plan de borrado incluyera
alguna, aborta antes de ejecutar nada.
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))

from lake_counts import connect  # noqa: E402

KEEP_BRONZE_PREFIXES: tuple[str, ...] = ("mssql_bs_", "mssql_d_mart_")
KEEP_BRONZE_TABLES: frozenset[str] = frozenset(
    {"_dlt_loads", "_dlt_pipeline_state", "_dlt_version"}
)
DROP_SCHEMAS: tuple[str, ...] = (
    "bronze_fixture",
    "bronze_staging",
    "staging_mssql",
    "silver",
    "gold",
)


def _existing_schemas(con) -> set[str]:
    rows = con.execute(
        "SELECT DISTINCT table_schema FROM information_schema.tables"
    ).fetchall()
    return {row[0] for row in rows}


def _bronze_tables(con) -> list[str]:
    rows = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'bronze' ORDER BY table_name"
    ).fetchall()
    return [row[0] for row in rows]


def is_protected(table: str) -> bool:
    """True si la tabla de bronze es una de las que hay que conservar."""
    return table in KEEP_BRONZE_TABLES or table.startswith(KEEP_BRONZE_PREFIXES)


def build_plan(con) -> tuple[list[str], list[str]]:
    """Devuelve (tablas de bronze a borrar, esquemas a borrar)."""
    tables_to_drop = [t for t in _bronze_tables(con) if not is_protected(t)]
    schemas_present = _existing_schemas(con)
    schemas_to_drop = [s for s in DROP_SCHEMAS if s in schemas_present]

    leaked = [t for t in tables_to_drop if is_protected(t)]
    if leaked:
        raise RuntimeError(f"Plan inválido: incluye tablas reales protegidas {leaked}")
    return tables_to_drop, schemas_to_drop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purga la data de ejemplo del DuckLake (solo quedan las 7 tablas MSSQL)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="ejecuta el borrado (sin este flag solo muestra el plan)",
    )
    args = parser.parse_args(argv)

    con = connect()
    tables_to_drop, schemas_to_drop = build_plan(con)

    if not tables_to_drop and not schemas_to_drop:
        print("Nada que purgar: el lago ya solo tiene tablas MSSQL reales.")
        return 0

    print("Plan de purga:")
    for table in tables_to_drop:
        count = con.execute(f'SELECT COUNT(*) FROM lake."bronze"."{table}"').fetchone()[0]
        print(f"  DROP TABLE  bronze.{table}  ({count} filas)")
    for schema in schemas_to_drop:
        print(f"  DROP SCHEMA {schema} CASCADE")

    if not args.apply:
        print("\nDry-run: nada borrado. Re-ejecutá con --apply para aplicar.")
        return 0

    for table in tables_to_drop:
        con.execute(f'DROP TABLE IF EXISTS lake."bronze"."{table}"')
    for schema in schemas_to_drop:
        con.execute(f'DROP SCHEMA IF EXISTS lake."{schema}" CASCADE')

    remaining = _bronze_tables(con)
    leftover = _existing_schemas(con) & set(DROP_SCHEMAS)
    print("\nPurga aplicada.")
    print(f"Tablas bronze restantes: {', '.join(remaining)}")
    if leftover:
        raise RuntimeError(f"Esquemas que sobrevivieron a la purga: {sorted(leftover)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
