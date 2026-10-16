#!/usr/bin/env python3
"""Benchmark de ingesta + queries (OpenSpec poc-data-lake-ducklake, 7.2/7.4).

Mide sobre el MISMO lago (catálogo postgres + Parquet en Garage):
  - generación sintética (`source/generate.py`, CSV a disco, cronometrada)
  - ingesta al lago (`CREATE TABLE bench.<t> AS FROM read_csv(...)`,
    cronometrada; ejerce el path DuckDB -> S3 + commit de catálogo)
  - Q1 agregación por mes/moneda (scan completo payments)
  - Q2 join payments->leases->customers con filtro temporal (top clientes)
  - Q3 filtro alta selectividad (lookup puntual + rango updated_at)

Cada query corre en conexión nueva (fría a nivel DuckDB). Escalas reales:
demo (100 clientes), base (10k) y mid (100k). Los perfiles 10gb/50gb se
EXTRAPOLAN por regresión lineal y se marcan como tales: materializarlos
requeriría ~31M/~155M filas en memoria en `generate.py` (OOM en el host,
ver findings). Solo datos sintéticos.

Uso: bronze/.venv/bin/python scripts/benchmark.py [--scales demo,base,mid]
Salida: JSON a stdout + tabla markdown. Limpia sus esquemas al terminar.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "scripts")
from lake_counts import connect  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCALES = {"demo": 100, "base": 10_000, "mid": 100_000}
TABLES = ("customers", "units", "leases", "payments")


def bucket_bytes(prefix: str) -> int:
    import s3fs

    fs = s3fs.S3FileSystem(
        key=os.getenv("GARAGE_S3_KEY_ID",
                      "GK0123456789abcdef0123456789abcdef"),
        secret=os.getenv("GARAGE_S3_SECRET",
                         "0123456789abcdef0123456789abcdef"
                         "0123456789abcdef0123456789abcdef"),
        client_kwargs={
            "endpoint_url": os.getenv("GARAGE_S3_ENDPOINT",
                                      "http://localhost:3900"),
            "region_name": os.getenv("GARAGE_S3_REGION", "garage")},
        config_kwargs={"s3": {"addressing_style": "path"}},
    )
    data_path = os.getenv("DUCKLAKE_DATA_PATH",
                          "s3://lakehouse/data/").rstrip("/")
    objs = fs.find(f"{data_path}/{prefix}")
    return sum(fs.info(o).get("size", 0) for o in objs)


def run_scale(rows: int, tag: str) -> dict:
    res: dict = {"clientes": rows, "filas": {}}
    workdir = f"/tmp/opencode/bench-{tag}"
    shutil.rmtree(workdir, ignore_errors=True)

    t0 = time.time()
    subprocess.run(
        [sys.executable, f"{ROOT}/source/generate.py", "--mode", "seed",
         "--rows", str(rows), "--out", "csv", "--out-dir", workdir],
        check=True, capture_output=True, text=True)
    res["t_gen_s"] = round(time.time() - t0, 2)
    res["csv_bytes"] = sum(
        os.path.getsize(os.path.join(workdir, f"{t}.csv")) for t in TABLES)

    schema = f"bench_{tag}"
    con = connect()
    con.execute("USE lake")
    con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    con.execute(f"CREATE SCHEMA {schema}")
    t0 = time.time()
    for t in TABLES:
        con.execute(
            f"CREATE TABLE {schema}.{t} AS "
            f"FROM read_csv('{workdir}/{t}.csv', auto_detect=true)")
        res["filas"][t] = con.execute(
            f"SELECT COUNT(*) FROM {schema}.{t}").fetchone()[0]
    res["t_ingesta_s"] = round(time.time() - t0, 2)
    res["parquet_bytes"] = bucket_bytes(schema)
    total = sum(res["filas"].values())
    res["filas_total"] = total
    res["rows_por_s"] = round(total / res["t_ingesta_s"], 1)
    con.close()

    p = f"lake.{schema}"
    queries = {
        "q1_agg_mes_moneda":
            f"SELECT date_trunc('month', paid_at)::DATE, currency, "
            f"SUM(amount), COUNT(*) FROM {p}.payments "
            f"GROUP BY 1, 2 ORDER BY 1, 2",
        "q2_top_clientes_6m":
            f"SELECT c.customer_id, SUM(pay.amount) AS tot, COUNT(*) AS n "
            f"FROM {p}.payments pay JOIN {p}.leases l "
            f"ON pay.lease_id = l.lease_id JOIN {p}.customers c "
            f"ON l.customer_id = c.customer_id "
            f"WHERE pay.paid_at > now() - INTERVAL '6 months' "
            f"GROUP BY 1 ORDER BY tot DESC LIMIT 10",
        "q3_lookup_puntual":
            f"SELECT * FROM {p}.payments WHERE payment_id = {total // 2}",
    }
    res["queries"] = {}
    for name, sql in queries.items():
        c2 = connect()
        t0 = time.time()
        out = c2.execute(sql).fetchall()
        res["queries"][name] = {"s": round(time.time() - t0, 3),
                                "filas_devueltas": len(out)}
        c2.close()

    con = connect()
    con.execute("USE lake")
    con.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    con.close()
    shutil.rmtree(workdir, ignore_errors=True)
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark ingesta + queries")
    ap.add_argument("--scales", default="demo,base,mid")
    args = ap.parse_args(argv)
    out = {"host": {}, "escalas": {}}
    try:
        mem = open("/proc/meminfo").readline().split()
        out["host"] = {"cpu_n": os.cpu_count(), "mem_total": mem[1] + mem[2]}
    except OSError:
        out["host"] = {"cpu_n": os.cpu_count()}
    for tag in [s.strip() for s in args.scales.split(",") if s.strip()]:
        out["escalas"][tag] = run_scale(SCALES[tag], tag)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
