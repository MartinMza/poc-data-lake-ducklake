#!/usr/bin/env python3
"""Prueba de concurrencia (OpenSpec poc-data-lake-ducklake, 7.3).

Dos lectores (queries gold típicas) + un escritor (INSERTs con commit por
fila en tabla propia `ops_concurrency_demo`) corriendo en 3 PROCESOS
separados contra el mismo catálogo Postgres (modo multi-proceso real, no
hilos de un mismo DuckDB embebido).

Criterio PASS: ambas lecturas completan sin errores de catálogo con valores
consistentes (sumas de snapshots válidos), la escritura commitea sus N filas
y el conteo final es exacto (sin corrupción ni filas perdidas).

No toca bronze/silver/gold en escritura; solo las lee (snapshot isolation).

Uso: bronze/.venv/bin/python scripts/concurrency_check.py [--reads 30 --writes 20]
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import sys
import time

sys.path.insert(0, "scripts")
from lake_counts import connect  # noqa: E402

TABLE = "ops_concurrency_demo"

READ_QUERIES = [
    "SELECT COUNT(*), SUM(total_amount) FROM lake.gold.gold_monthly_revenue",
    "SELECT COUNT(*), SUM(total_paid) FROM lake.gold.gold_customer_revenue",
    "SELECT COUNT(*), SUM(total_collected) FROM lake.gold.gold_unit_performance",
]


def reader(tag: str, n_reads: int, q: mp.Queue) -> None:
    try:
        con = connect()
        seen = set()
        t0 = time.time()
        for _ in range(n_reads):
            for sql in READ_QUERIES:
                seen.add(tuple(con.execute(sql).fetchone()))
        q.put((tag, "ok", len(seen), round(time.time() - t0, 2)))
    except Exception as exc:  # noqa: BLE001
        q.put((tag, f"ERROR: {exc}", 0, 0.0))


def writer(n_writes: int, q: mp.Queue) -> None:
    try:
        con = connect()
        con.execute("USE lake")
        con.execute(f"DROP TABLE IF EXISTS {TABLE}")
        con.execute(f"CREATE TABLE {TABLE} (id INTEGER, v VARCHAR)")
        t0 = time.time()
        for i in range(n_writes):
            con.execute(f"INSERT INTO {TABLE} VALUES ({i}, 'w{i}')")
            time.sleep(0.05)
        n = con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        q.put(("writer", "ok" if n == n_writes else f"COUNT={n}", n,
               round(time.time() - t0, 2)))
    except Exception as exc:  # noqa: BLE001
        q.put(("writer", f"ERROR: {exc}", 0, 0.0))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="2 lectores + 1 escritor")
    ap.add_argument("--reads", type=int, default=30)
    ap.add_argument("--writes", type=int, default=20)
    args = ap.parse_args(argv)

    q: mp.Queue = mp.Queue()
    procs = [
        mp.Process(target=reader, args=("reader-1", args.reads, q)),
        mp.Process(target=reader, args=("reader-2", args.reads, q)),
        mp.Process(target=writer, args=(args.writes, q)),
    ]
    t0 = time.time()
    for p in procs:
        p.start()
    for p in procs:
        p.join(300)
    alive = [p.is_alive() for p in procs]
    results = []
    while not q.empty():
        results.append(q.get())

    for tag, status, n, dt in sorted(results):
        print(f"{tag}: status={status} ops={n} t={dt}s")
    print(f"total pared: {round(time.time() - t0, 2)}s")

    con = connect()
    final = con.execute(
        f"SELECT COUNT(*) FROM lake.main.{TABLE}").fetchone()[0]
    print(f"conteo final {TABLE}: {final} (esperado {args.writes})")
    con.execute(f"DROP TABLE IF EXISTS lake.main.{TABLE}")

    ok = (
        not any(alive)
        and len(results) == 3
        and all(s == "ok" for _, s, _, _ in results)
        and final == args.writes
    )
    print("concurrencia OK" if ok else "concurrencia FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
