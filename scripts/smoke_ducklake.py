"""Smoke test DuckDB -> DuckLake (OpenSpec poc-data-lake-ducklake, tarea 2.3).

Verifica el criterio de aceptacion: DuckDB adjunta el DuckLake (catalogo en
postgres-catalog + data path en Garage/S3) y puede crear, escribir y leer
una tabla de prueba.

Uso:
    pip install -r scripts/requirements-smoke.txt   # duckdb==1.5.6 fijado
    python3 scripts/smoke_ducklake.py

Lee conexion desde .env (si existe) o de las mismas defaults que
docker-compose.yml. Exit 0 si la fila escrita se lee de vuelta; exit 1 si no.
Idempotente: la tabla de prueba se dropea al final (los Parquet ya escritos
son inmutables por diseno de DuckLake y quedan como snapshot history).
"""

import os
import sys

CATALOG_DB = os.getenv("CATALOG_DB", "ducklake_catalog")
CATALOG_USER = os.getenv("CATALOG_USER", "ducklake")
CATALOG_PASSWORD = os.getenv("CATALOG_PASSWORD", "ducklake")
CATALOG_HOST = os.getenv("CATALOG_HOST", "localhost")
CATALOG_PORT = os.getenv("CATALOG_PORT", "5433")
S3_KEY_ID = os.getenv(
    "GARAGE_S3_KEY_ID", "GK0123456789abcdef0123456789abcdef"
)
S3_SECRET = os.getenv(
    "GARAGE_S3_SECRET",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)
S3_HOST = os.getenv("GARAGE_HOST", "localhost")
S3_PORT = os.getenv("GARAGE_S3_PORT", "3900")
S3_ENDPOINT = os.getenv("GARAGE_S3_ENDPOINT", f"{S3_HOST}:{S3_PORT}")
S3_REGION = os.getenv("GARAGE_S3_REGION", "garage")
DATA_PATH = os.getenv("DUCKLAKE_DATA_PATH", "s3://lakehouse/data/")

TABLE = "infra_smoke_test"
EXPECTED = [(1, "hola"), (2, "ducklake")]


def main() -> int:
    # .env opcional para no exigir export manual (formato KEY=VALUE simple).
    if os.path.exists(".env"):
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        # Releer por si .env definia valores (los ya fijados arriba quedan).
        global CATALOG_DB, CATALOG_USER, CATALOG_PASSWORD, CATALOG_HOST, CATALOG_PORT, S3_HOST, S3_PORT, S3_ENDPOINT  # noqa: PLW0603
        CATALOG_DB = os.getenv("CATALOG_DB", CATALOG_DB)
        CATALOG_USER = os.getenv("CATALOG_USER", CATALOG_USER)
        CATALOG_PASSWORD = os.getenv("CATALOG_PASSWORD", CATALOG_PASSWORD)
        CATALOG_HOST = os.getenv("CATALOG_HOST", CATALOG_HOST)
        CATALOG_PORT = os.getenv("CATALOG_PORT", CATALOG_PORT)
        S3_HOST = os.getenv("GARAGE_HOST", S3_HOST)
        S3_PORT = os.getenv("GARAGE_S3_PORT", S3_PORT)
        S3_ENDPOINT = os.getenv("GARAGE_S3_ENDPOINT", f"{S3_HOST}:{S3_PORT}")

    try:
        import duckdb
    except ImportError:
        print(
            "error: falta el paquete duckdb; "
            "instalar con: pip install -r scripts/requirements-smoke.txt",
            file=sys.stderr,
        )
        return 1

    catalog_dsn = (
        f"dbname={CATALOG_DB} host={CATALOG_HOST} "
        f"port={CATALOG_PORT} user={CATALOG_USER} password={CATALOG_PASSWORD}"
    )
    endpoint_host = S3_ENDPOINT.removeprefix("http://").removeprefix(
        "https://"
    )

    con = duckdb.connect()
    con.execute("INSTALL ducklake; LOAD ducklake;")
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE OR REPLACE SECRET garage_s3 (
          TYPE S3,
          KEY_ID '{S3_KEY_ID}',
          SECRET '{S3_SECRET}',
          ENDPOINT '{endpoint_host}',
          URL_STYLE 'path',
          USE_SSL false,
          REGION '{S3_REGION}'
        );
        """
    )
    try:
        con.execute(
            f"ATTACH 'ducklake:postgres:{catalog_dsn}' AS lake "
            f"(DATA_PATH '{DATA_PATH}')"
        )
    except duckdb.Error as exc:
        # El lago ya existe con otro DATA_PATH canonico: el attach lo carga
        # desde el catalogo (verificado contra docs DuckLake "Connecting").
        if "does not match existing data path" in str(exc):
            print(f"aviso: {exc}; re-adjuntando al DATA_PATH canonico")
            con.execute(
                f"ATTACH 'ducklake:postgres:{catalog_dsn}' AS lake"
            )
        else:
            raise
    con.execute("USE lake")

    con.execute(f"DROP TABLE IF EXISTS {TABLE}")
    con.execute(f"CREATE TABLE {TABLE} (id INTEGER, name VARCHAR)")
    con.execute(f"INSERT INTO {TABLE} VALUES (1, 'hola'), (2, 'ducklake')")
    rows = con.execute(
        f"SELECT id, name FROM {TABLE} ORDER BY id"
    ).fetchall()
    print(f"filas leidas: {rows}")
    if list(rows) != EXPECTED:
        print(f"error: se esperaba {EXPECTED}", file=sys.stderr)
        return 1
    con.execute(f"DROP TABLE {TABLE}")
    print("smoke OK: crear/escribir/leer contra DuckLake verificado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
