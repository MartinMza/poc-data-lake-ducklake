#!/usr/bin/env python3
"""Cargador Bronze: árbol Parquet del extractor -> esquema `bronze` en DuckLake.

Lee el contrato de salida del extractor (`<src>/<schema>/<table>.parquet`,
un archivo por tabla, compresión ZSTD) y carga cada tabla con `read_parquet`
de DuckDB en `bronze.mssql_*`. Reemplazo total por tabla (idempotente): cada
backup se extrae completo, así que no hay allowlist curada ni modos por tabla.

Nombres: el cargador normaliza a `snake_case` con prefijo `mssql_` para
conservar el contrato del lago (`dbo.BSTransaccion` -> `bronze.mssql_bs_transaccion`;
esquemas no-`dbo` agregan su esquema: `<schema>_<tabla>`). Las columnas se
normalizan igual (`TransaccionID` -> `transaccion_id`); si dos columnas
colisionan al normalizar, la tabla se aborta con mensaje explícito.

Conexión al lago: reutiliza `scripts/lake_counts.connect()` (mismo catálogo
postgres + mismo `DATA_PATH` S3 vía Garage, con SECRET y ATTACH canónicos).

Uso:
    bronze/.venv/bin/python bronze/load_parquet.py --src <dir> [--report-json <ruta>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import lake_counts  # noqa: E402


def snake(name: str) -> str:
    """Normaliza un identificador a `snake_case` (tablas y columnas).

    Compatible con el naming que usaba la ingesta anterior: `BSTransaccion`
    -> `bs_transaccion`, `DMart_VentasProductoCliente` ->
    `d_mart_ventas_producto_cliente`.
    """
    s = re.sub(r"[^0-9A-Za-z_]", "_", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def dest_table(schema: str, table: str) -> str:
    """Tabla destino en `bronze` para `<schema>/<table>.parquet`."""
    base = snake(table)
    if snake(schema) != "dbo":
        base = f"{snake(schema)}_{base}"
    return f"mssql_{base}"


def _ident(name: str) -> str:
    """Cita un identificador para SQL (escapa comillas dobles)."""
    return '"' + name.replace('"', '""') + '"'


def _lit(value: str) -> str:
    """Cita un literal de texto para SQL (escapa comillas simples)."""
    return "'" + value.replace("'", "''") + "'"


def parquet_columns(con, path: str) -> list[str]:
    """Nombres de columna del Parquet en orden, vía `DESCRIBE`."""
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet({_lit(path)})"
    ).fetchall()
    return [r[0] for r in rows]


def load_one(con, dest: str, path: str) -> int:
    """Reemplaza `bronze.<dest>` con el contenido del Parquet. Devuelve filas."""
    cols = parquet_columns(con, path)
    if not cols:
        raise SystemExit(f"error: {path} no trae columnas")
    normed = [snake(c) for c in cols]
    seen: set[str] = set()
    dupes = sorted({c for c in normed if c in seen or seen.add(c)})
    if dupes:
        raise SystemExit(
            f"error: {path} colisiona al normalizar a snake_case: {dupes}"
        )
    proj = ", ".join(f"{_ident(c)} AS {_ident(n)}" for c, n in zip(cols, normed))
    con.execute(
        f"CREATE OR REPLACE TABLE bronze.{_ident(dest)} AS "
        f"SELECT {proj} FROM read_parquet({_lit(path)})"
    )
    return int(
        con.execute(f"SELECT COUNT(*) FROM bronze.{_ident(dest)}").fetchone()[0]
    )


def discover(src: str) -> list[tuple[str, str, str]]:
    """Lista `(schema, tabla, path)` para `<src>/<schema>/<tabla>.parquet`."""
    found: list[tuple[str, str, str]] = []
    for schema in sorted(os.listdir(src)):
        sdir = os.path.join(src, schema)
        if not os.path.isdir(sdir):
            continue
        for fname in sorted(os.listdir(sdir)):
            if not fname.lower().endswith(".parquet"):
                continue
            found.append((schema, fname[: -len(".parquet")], os.path.join(sdir, fname)))
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Carga Parquet del extractor a Bronze (reemplazo total por tabla)"
    )
    ap.add_argument("--src", required=True, help="directorio salida del extractor")
    ap.add_argument("--report-json", default="", help="ruta del resumen JSON")
    args = ap.parse_args(argv)

    src = os.path.abspath(args.src)
    if not os.path.isdir(src):
        print(f"error: --src no existe o no es un directorio: {src}", file=sys.stderr)
        return 2
    planned = discover(src)
    if not planned:
        print(
            f"error: sin tablas en {src} (se esperaba <schema>/<tabla>.parquet)",
            file=sys.stderr,
        )
        return 2

    con = lake_counts.connect()
    con.execute("USE lake")
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")

    report: dict = {"src": src, "tables": {}}
    failed: list[str] = []
    total_rows = 0
    t0 = time.time()
    for schema, table, path in planned:
        dest = dest_table(schema, table)
        rel = os.path.relpath(path, src)
        try:
            start = time.time()
            rows = load_one(con, dest, path)
        except Exception as exc:  # noqa: BLE001 - se junta todo al resumen final
            failed.append(dest)
            print(f"load bronze.{dest} [desde {rel}]: FALLO: {exc}", flush=True)
            continue
        secs = time.time() - start
        total_rows += rows
        report["tables"][dest] = {
            "schema": schema,
            "table": table,
            "source": rel,
            "rows": rows,
            "seconds": round(secs, 1),
        }
        print(f"load bronze.{dest}: {rows} filas ({secs:.1f}s)", flush=True)

    total_secs = time.time() - t0
    report["total_tables"] = len(report["tables"])
    report["total_rows"] = total_rows
    report["total_seconds"] = round(total_secs, 1)
    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, sort_keys=True)
    print(
        f"BRONZE total: {total_rows} filas en {len(report['tables'])} tablas "
        f"({total_secs:.1f}s)"
    )
    if failed:
        print(
            f"error: {len(failed)} tabla(s) fallaron: {failed} "
            "(re-correr converge: cada tabla se reemplaza entera)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
