#!/usr/bin/env python3
"""Pipeline bronze: postgres-source -> esquema `bronze` en DuckLake (mismo lake).

OpenSpec `poc-data-lake-ducklake`, tareas 4.1-4.3.

- Destino nativo `dlt.destinations.ducklake` (dlt==1.31.0, verificado en
  specs/compat-matrix.md). Mismo DuckLake que el smoke de infra:
  mismo catalogo postgres + mismo DATA_PATH s3://lakehouse/data/
  (ver scripts/smoke_ducklake.py y scripts/README.md).
- `metadata_schema="public"`: el lake canonico creado por el smoke guarda
  su metadata en el schema postgres `public` (sin METADATA_SCHEMA explicito).
  dlt por defecto usaria `ducklake_name` como schema de metadata (= otro lake
  en la misma DB). Fijar `public` reutiliza el lake existente (ver docs dlt
  "Metadata schema": para catalogos existentes en `public`).
- `automatic_migration=True`: el catalogo creado con duckdb 1.4.4 (scripts/)
  es version 0.3; duckdb 1.5.6 (este venv, DuckLake 1.0) exige version 1.0.
  Sin este flag el attach falla con "catalog version mismatch". La migracion
  es irreversible y deja scripts/smoke_ducklake.py (duckdb 1.4.4) sin leer el
  catalogo -> BLOQUEO reportado en bronze/README.md (infra debe subir a 1.5.6).
- Incremental por `updated_at` con merge/upsert por PK (sin duplicados).
  `initial_value` es datetime con tz (no string) — gotcha verificado.
- `catalog` sin sufijo de driver (`postgresql://...`, NO `+psycopg2`);
  el sufijo solo va en la URL del ORIGEN `sql_table` (sqlalchemy).
- `storage` anidado como {"bucket_url": ...} + credentials S3 (Garage);
  la forma plana como string se ignora en silencio (gotcha verificado).
- Metadatos de ingesta: dlt aporta `_dlt_load_id` / `_dlt_id` por fila.

Uso:
    bronze/.venv/bin/python bronze/pipeline.py [--full-refresh] [--tables customers,units]
    # env: ver bronze/README.md (mismos defaults que docker-compose/.env.example)

Solo datos sinteticos (origen simulado). Sin credenciales en el repo.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

TABLES: dict[str, str] = {
    "customers": "customer_id",
    "units": "unit_id",
    "leases": "lease_id",
    "payments": "payment_id",
}

EARLY = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _load_dotenv() -> None:
    if os.path.exists(".env"):
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def build_urls() -> tuple[str, str, str, dict]:
    _load_dotenv()
    cat_user = _env("CATALOG_USER", "ducklake")
    cat_pass = _env("CATALOG_PASSWORD", "ducklake")
    cat_host = _env("CATALOG_HOST", "localhost")
    cat_port = _env("CATALOG_PORT", "5433")
    cat_db = _env("CATALOG_DB", "ducklake_catalog")
    # Sin sufijo de driver (gotcha: +psycopg2 falla con NotImplementedError).
    catalog_url = f"postgresql://{cat_user}:{cat_pass}@{cat_host}:{cat_port}/{cat_db}"

    src_user = _env("SOURCE_USER", "source_user")
    src_pass = _env("SOURCE_PASSWORD", "source_password")
    src_host = _env("SOURCE_HOST", "localhost")
    src_port = _env("SOURCE_PORT", "5434")
    src_db = _env("SOURCE_DB", "source_db")
    # El origen SI lleva driver sqlalchemy.
    source_url = (
        f"postgresql+psycopg2://{src_user}:{src_pass}@{src_host}:{src_port}/{src_db}"
    )

    data_path = _env("DUCKLAKE_DATA_PATH", "s3://lakehouse/data/")
    if not data_path.endswith("/"):
        data_path += "/"

    garage_scheme = _env("GARAGE_SCHEME", "http")
    garage_host = _env("GARAGE_HOST", "localhost")
    garage_port = _env("GARAGE_S3_PORT", "3900")
    s3_creds = {
        "aws_access_key_id": _env(
            "GARAGE_S3_KEY_ID", "GK0123456789abcdef0123456789abcdef"
        ),
        "aws_secret_access_key": _env(
            "GARAGE_S3_SECRET",
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        ),
        "endpoint_url": _env(
            "GARAGE_S3_ENDPOINT", f"{garage_scheme}://{garage_host}:{garage_port}"
        ),
        "region_name": _env("GARAGE_S3_REGION", "garage"),
        "s3_url_style": "path",
    }
    return catalog_url, source_url, data_path, s3_creds


def make_pipeline(pipeline_name: str, dataset: str = "bronze"):
    import dlt

    catalog_url, _, data_path, s3_creds = build_urls()
    ducklake_name = _env("DUCKLAKE_NAME", "lake")
    metadata_schema = _env("DUCKLAKE_METADATA_SCHEMA", "public")
    dest = dlt.destinations.ducklake(
        credentials={
            "ducklake_name": ducklake_name,
            "metadata_schema": metadata_schema,
            "catalog": catalog_url,
            # Anidado (gotcha: string plano se ignora en silencio).
            "storage": {"bucket_url": data_path, "credentials": s3_creds},
        },
        automatic_migration=True,
    )
    return dlt.pipeline(
        pipeline_name=pipeline_name,
        destination=dest,
        dataset_name=dataset,
        dev_mode=False,
    )


def make_resources(source_url: str, tables: list[str]):
    import dlt
    from dlt.sources.sql_database import sql_table

    resources = []
    for name in tables:
        pk = TABLES[name]
        res = sql_table(
            credentials=source_url,
            schema="public",
            table=name,
            incremental=dlt.sources.incremental(
                "updated_at", initial_value=EARLY
            ),
            write_disposition={
                "disposition": "merge",
                "strategy": "upsert",
                "primary_key": pk,
            },
        )
        resources.append(res)
    return resources


def fetch_counts(pipe, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with pipe.sql_client() as client:
        for t in tables:
            rows = client.execute_sql(f"SELECT COUNT(*) FROM bronze.{t}")
            counts[t] = int(rows[0][0])
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingesta bronze postgres->DuckLake (dlt)")
    ap.add_argument("--tables", default=",".join(TABLES),
                    help="subset coma-separado (default: las 4)")
    ap.add_argument("--full-refresh", action="store_true",
                    help="dropea el dataset bronze antes de cargar (carga inicial limpia)")
    ap.add_argument("--pipeline-name", default=os.getenv("PIPELINE_NAME", "bronze_ingestion"))
    args = ap.parse_args(argv)

    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    unknown = [t for t in tables if t not in TABLES]
    if unknown:
        print(f"tablas desconocidas: {unknown} (validas: {sorted(TABLES)})", file=sys.stderr)
        return 2

    _, source_url, _, _ = build_urls()
    pipe = make_pipeline(args.pipeline_name)

    if args.full_refresh:
        print("--full-refresh: dropeando dataset bronze ...")
        try:
            pipe.drop()
            pipe = make_pipeline(args.pipeline_name)
        except Exception as exc:  # noqa: BLE001 - dlt puede no tener nada que dropear
            print(f"aviso drop: {exc}")

    resources = make_resources(source_url, tables)
    print(f"pipeline={args.pipeline_name} dataset=bronze tablas={tables}")
    info = pipe.run(resources)
    print(info)

    counts = fetch_counts(pipe, tables)
    total = sum(counts.values())
    for t in tables:
        print(f"bronze.{t}: {counts[t]} filas")
    print(f"TOTAL bronze: {total} filas")
    # Metadatos de ingesta (muestra): _dlt_load_id presente en cada tabla.
    with pipe.sql_client() as client:
        for t in tables:
            sample = client.execute_sql(
                f"SELECT _dlt_load_id FROM bronze.{t} LIMIT 1"
            )
            print(f"bronze.{t} _dlt_load_id ejemplo: {sample[0][0] if sample else 'VACIA'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
