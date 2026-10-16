#!/usr/bin/env python3
"""Export idempotente gold (DuckLake) -> Postgres (OpenSpec 7.5).

Decisión documentada en docs/findings.md §7.5: SÍ se exporta (criterio:
compatibilidad BI vía protocolo Postgres vs DuckDB embebido; costo mínimo
sin infra nueva; refresco transaccional sobre agregados pequeños).

Destino: base `gold_serving` en la MISMA instancia `postgres-catalog`
(se crea si falta; no toca `ducklake_catalog` ni `postgres-source` ni el
compose). Por tabla: escribe `t_next` y hace swap atómico
(DROP t + RENAME en una transacción). Re-correr deja contenido idéntico,
sin duplicados (el swap reemplaza, no acumula).

Uso: bronze/.venv/bin/python scripts/export_gold.py [--db gold_serving]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, "scripts")
from lake_counts import connect  # noqa: E402

GOLD_TABLES = (
    "gold_monthly_revenue",
    "gold_customer_revenue",
    "gold_unit_performance",
)


def _pg_dsn(db: str) -> str:
    u = os.getenv("CATALOG_USER", "ducklake")
    pw = os.getenv("CATALOG_PASSWORD", "ducklake")
    h = os.getenv("CATALOG_HOST", "localhost")
    p = os.getenv("CATALOG_PORT", "5433")
    return f"postgresql://{u}:{pw}@{h}:{p}/{db}"


def main(argv: list[str] | None = None) -> int:
    import psycopg  # del venv bronze (ya fijado para el seed)

    ap = argparse.ArgumentParser(description="Export gold -> Postgres")
    ap.add_argument("--db", default="gold_serving")
    args = ap.parse_args(argv)

    con = connect()
    payload: dict[str, tuple[list[tuple[str, str]], list[tuple]]] = {}
    for t in GOLD_TABLES:
        cols = [(r[0], r[1]) for r in
                con.execute(f"DESCRIBE SELECT * FROM lake.gold.{t}").fetchall()]
        rows = con.execute(f"SELECT * FROM lake.gold.{t}").fetchall()
        payload[t] = (cols, rows)
    con.close()

    with psycopg.connect(_pg_dsn("postgres"), autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (args.db,)
        ).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{args.db}"')
            print(f"base {args.db}: creada")

    with psycopg.connect(_pg_dsn(args.db)) as pg:
        for t, (cols, rows) in payload.items():
            coldef = ", ".join(f'"{n}" {typ}' for n, typ in cols)
            names = ", ".join(f'"{n}"' for n, _ in cols)
            with pg.cursor() as cur:
                cur.execute(f'DROP TABLE IF EXISTS "{t}_next"')
                cur.execute(f'CREATE TABLE "{t}_next" ({coldef})')
                if rows:
                    cur.executemany(
                        f'INSERT INTO "{t}_next" ({names}) VALUES '
                        f"({', '.join(['%s'] * len(cols))})", rows)
                # Swap atómico: los lectores nunca ven tabla vacía/a medias.
                cur.execute("BEGIN")
                cur.execute(f'DROP TABLE IF EXISTS "{t}"')
                cur.execute(f'ALTER TABLE "{t}_next" RENAME TO "{t}"')
                cur.execute("COMMIT")
            n = pg.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            print(f"{t}: lago={len(rows)} postgres={n} "
                  f"{'OK' if n == len(rows) else 'DIF'}")
            assert n == len(rows), t
    print("export gold OK (idempotente: re-correr reemplaza, no duplica)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
