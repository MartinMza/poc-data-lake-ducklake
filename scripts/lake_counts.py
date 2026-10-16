#!/usr/bin/env python3
"""Conteos bronze/silver/gold del DuckLake (JSON a stdout).

OpenSpec `poc-data-lake-ducklake`, tareas 7.1/7.4. Helper compartido por
`scripts/backup.sh` (manifiesto), `scripts/restore.sh` (verificación),
benchmark y prueba de concurrencia. Solo lectura.

Uso (con el venv que tenga duckdb fijado, p. ej. bronze/.venv):
    bronze/.venv/bin/python scripts/lake_counts.py [--schemas bronze,silver,gold]

Env: mismos defaults que docker-compose.yml/.env.example (CATALOG_*,
GARAGE_S3_*, DUCKLAKE_DATA_PATH).
"""

from __future__ import annotations

import argparse
import json
import os


def _env(name: str, default: str) -> str:
    v = os.getenv(name, default)
    if os.path.exists(".env") and name not in os.environ:
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, val = line.split("=", 1)
                    if k.strip() == name:
                        v = val.strip()
                        break
    return v


def connect():
    import duckdb

    cat = (
        f"dbname={_env('CATALOG_DB', 'ducklake_catalog')} "
        f"host={_env('CATALOG_HOST', 'localhost')} "
        f"port={_env('CATALOG_PORT', '5433')} "
        f"user={_env('CATALOG_USER', 'ducklake')} "
        f"password={_env('CATALOG_PASSWORD', 'ducklake')}"
    )
    endpoint = _env("GARAGE_S3_ENDPOINT", "http://localhost:3900")
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
    except Exception as exc:  # noqa: BLE001 - DATA_PATH canonico del catalogo
        if "does not match existing data path" in str(exc):
            con.execute(f"ATTACH 'ducklake:postgres:{cat}' AS lake")
        else:
            raise
    return con


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Conteos del lago (JSON)")
    ap.add_argument("--schemas", default="bronze,silver,gold")
    args = ap.parse_args(argv)
    con = connect()
    out: dict[str, int] = {}
    for schema in [s.strip() for s in args.schemas.split(",") if s.strip()]:
        tables = con.execute(
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema='{schema}' ORDER BY 1"
        ).fetchall()
        for (t,) in tables:
            out[f"{schema}.{t}"] = con.execute(
                f'SELECT COUNT(*) FROM lake."{schema}"."{t}"'
            ).fetchone()[0]
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
