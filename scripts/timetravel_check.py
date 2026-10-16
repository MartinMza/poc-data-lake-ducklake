#!/usr/bin/env python3
"""Prueba de time travel / snapshot (OpenSpec poc-data-lake-ducklake, 7.2).

Crea una tabla propia `ops_timetravel_demo` (no toca bronze/silver/gold):
  1. registra snapshot base, inserta 3 filas -> snapshot V1
  2. DELETE 1 fila + UPDATE 1 fila -> snapshot V2
  3. consulta V1 con la sentencia exacta `... AT (VERSION => V1)` y verifica
     que devuelve las 3 filas originales, mientras la lectura actual da 2.

Sentencias exactas y resultado quedan impresos (evidencia para findings).
La tabla se dropea al final; los Parquet históricos quedan (inmutables).

Uso: bronze/.venv/bin/python scripts/timetravel_check.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "scripts")
from lake_counts import connect  # noqa: E402

TABLE = "ops_timetravel_demo"


def snap(con) -> int:
    return con.execute("FROM ducklake_current_snapshot('lake')").fetchone()[0]


def main() -> int:
    con = connect()
    con.execute("USE lake")
    con.execute(f"DROP TABLE IF EXISTS {TABLE}")

    v0 = snap(con)
    con.execute(
        f"CREATE TABLE {TABLE} (id INTEGER, name VARCHAR, amount DECIMAL(12,2))"
    )
    con.execute(
        f"INSERT INTO {TABLE} VALUES "
        "(1, 'alquiler-ene', 45000.00), "
        "(2, 'alquiler-feb', 47000.00), "
        "(3, 'alquiler-mar', 47000.00)"
    )
    v1 = snap(con)
    current = con.execute(f"SELECT * FROM {TABLE} ORDER BY id").fetchall()
    assert len(current) == 3, current

    con.execute(f"DELETE FROM {TABLE} WHERE id = 3")
    con.execute(f"UPDATE {TABLE} SET amount = 99999.00 WHERE id = 2")
    v2 = snap(con)

    q_hist = f"SELECT * FROM lake.main.{TABLE} AT (VERSION => {v1}) ORDER BY id"
    hist = con.execute(q_hist).fetchall()
    now = con.execute(f"SELECT * FROM {TABLE} ORDER BY id").fetchall()

    print(f"snapshot base V0={v0}, tras INSERT V1={v1}, tras DELETE+UPDATE V2={v2}")
    print(f"histórica [{q_hist}]:")
    for r in hist:
        print("  ", r)
    print(f"actual [SELECT * FROM {TABLE} ORDER BY id]:")
    for r in now:
        print("  ", r)

    ok = (
        len(hist) == 3
        and hist[1][2] == 47000.00  # amount original de id=2 en V1
        and len(now) == 2
        and now[1][2] == 99999.00  # amount modificado hoy
        and v1 > v0
        and v2 > v1
    )
    con.execute(f"DROP TABLE {TABLE}")
    print("time travel OK" if ok else "time travel FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
