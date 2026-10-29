#!/usr/bin/env python3
"""Pipeline bronze (fallback dlt): SQL Server restaurado -> esquema `bronze` en DuckLake.

Fallback/rollback del path rapido `bronze/fast_mssql.py` (principal). Ingesta
las 7 tablas reales de SQL Server (`FAF12_Kavos`) al esquema `bronze` del
DuckLake:
- MISMO DuckLake: mismo catalogo postgres + mismo DATA_PATH
  (attach canonico, `metadata_schema="public"`, `automatic_migration=True`).
- Fuente `sql_database` de dlt con dialecto `mssql` via pyodbc + ODBC Driver 18.
- Tablas destino con prefijo `mssql_` en el esquema `bronze`
  (dlt snake_casea: `BSMoneda` -> `mssql_bs_moneda`, etc.).
- Estrategia por tabla (ver MSSQL_TABLES): `merge`/upsert por PK (default con
  PK), `replace` (sin PK, p.ej. `DMart_*`), `incremental` (legacy, solo si la
  tabla trae un `updated_at` mantenido).
- Metadatos de ingesta: dlt aporta `_dlt_load_id` / `_dlt_id` por fila.

Driver (decision 3 del design): pyodbc + Microsoft ODBC Driver 18 instalado
en user-space (`~/.local/mssql-odbc`, sin root). Este script auto-configura
`LD_LIBRARY_PATH`/`ODBCSYSINI` si no estan seteadas (ver bronze/README.md).

Uso:
    bronze/.venv/bin/python bronze/pipeline_mssql.py [--full-refresh] [--tables BSMoneda,BSProducto]
    # env: MSSQL_HOST/PORT/DATABASE/USER + MSSQL_PASSWORD (fallback MSSQL_SA_PASSWORD).
    # Default: MSSQL_DATABASE=FAF12_Kavos + las 7 tablas reales (DEFAULT_MSSQL_TABLES).
    # Override por env MSSQL_TABLES="Tab:pk:merge,..." (+ MSSQL_PIPELINE_NAME para
    #   estado dlt separado). Sin credenciales en el repo (.env, gitignored).

Solo-PoC-local. No toca `orchestration/` ni `docker-compose.yml`.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from urllib.parse import quote_plus


def _bootstrap_odbc_env() -> None:
    """Deja pyodbc/ODBC-18 operativo sin root ni exports manuales.

    El Driver Manager (unixODBC) y el driver MS viven en user-space
    (`~/.local/mssql-odbc`). Como el loader solo lee LD_LIBRARY_PATH al
    arranque del proceso, se precargan libodbc/libodbcinst por ruta absoluta
    (ctypes RTLD_GLOBAL) antes de que pyodbc haga dlopen. ODBCSYSINI si se
    puede setear in-process (unixODBC lo lee en cada conexion).
    """
    home = os.path.expanduser("~")
    default_base = os.path.join(home, ".local", "mssql-odbc")
    libdir = os.path.join(default_base, "usr", "lib", "x86_64-linux-gnu")
    for lib in ("libodbcinst.so.2", "libodbc.so.2"):
        path = os.path.join(libdir, lib)
        if os.path.isfile(path):
            try:
                import ctypes
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass
    os.environ.setdefault("LD_LIBRARY_PATH", libdir)
    ini_dir = os.path.join(default_base, "odbc-ini")
    if os.path.isfile(os.path.join(ini_dir, "odbcinst.ini")):
        os.environ.setdefault("ODBCSYSINI", ini_dir)


_bootstrap_odbc_env()

SOURCE_SCHEMA = "dbo"
TABLE_PREFIX = "mssql_"

# Cursor inicial naive para la estrategia incremental legacy (si alguna tabla
# se pide con `updated_at` DATETIME2 sin tz via MSSQL_TABLES="...:pk:incremental").
EARLY = datetime(2020, 1, 1)

# Default real: las 7 tablas de SQL Server (`FAF12_Kavos`). Unica fuente de la
# lista; `bronze/fast_mssql.py` la importa en vez de duplicar la constante.
DEFAULT_MSSQL_TABLES = (
    "BSMoneda:MonedaID:merge,BSProducto:ProductoID:merge,"
    "BSOrganizacion:OrganizacionID:merge,BSTransaccion:TransaccionID:merge,"
    "BSOperacion:TransaccionID:merge,DMart_Ventas::replace,"
    "DMart_VentasProductoCliente::replace"
)

# Tipos MSSQL aceptados sin perdida (spec: mapeo sin perdida + fallo claro
# ante tipo no soportado). Verificado en FAF12_Kavos: MONEY/DECIMAL/NUMERIC,
# DATETIME/DATETIME2, BIT, VARCHAR/NVARCHAR (+ BIGINT/INT/SMALLINT/DATE).
# TEXT/NTEXT (legacy LOB; pyodbc los devuelve como str -> VARCHAR, sin
# perdida para el PoC; tablas grandes con IMAGE/XML/GEOGRAPHY siguen
# abortando en check_source_types hasta ampliar el mapeo caso por caso).
SUPPORTED_TYPES = frozenset({
    "bigint", "int", "smallint", "tinyint", "bit",
    "decimal", "numeric", "money", "smallmoney", "float", "real",
    "char", "varchar", "nchar", "nvarchar", "text", "ntext",
    "datetime", "datetime2", "smalldatetime", "date", "time",
    "uniqueidentifier",
})

# El esquema real no trae updated_at ni rowversion en ninguna tabla (verificado
# en FAF12_Kavos: 0 columnas rowversion en 779 tablas), asi que la estrategia
# por tabla se elige via MSSQL_TABLES="Nombre:pk[:estrategia],..." donde
# estrategia es:
#   merge         full-extract + upsert por PK, sin cursor (default si hay pk)
#   replace       recarga total, sin PK (default si pk vacio; p.ej. DMart_*)
#   incremental   cursor updated_at + merge (solo si la tabla trae updated_at
#                 mantenido; ninguna de las 7 reales lo usa)
# Sin MSSQL_TABLES se usa DEFAULT_MSSQL_TABLES (las 7 tablas reales).
TABLES_ENV_VAR = "MSSQL_TABLES"


def effective_tables() -> tuple[dict[str, str], dict[str, str]]:
    """Devuelve (tablas {nombre: pk}, estrategias {nombre: estrategia}).

    Sin MSSQL_TABLES: default real (las 7 tablas de `FAF12_Kavos`).
    """
    spec = os.getenv(TABLES_ENV_VAR, "").strip() or DEFAULT_MSSQL_TABLES
    tables: dict[str, str] = {}
    strategies: dict[str, str] = {}
    for entry in spec.split(","):
        parts = [p.strip() for p in entry.split(":")]
        name = parts[0] if parts else ""
        pk = parts[1] if len(parts) > 1 else ""
        strat = (parts[2] if len(parts) > 2 and parts[2] else
                 ("merge" if pk else "replace")).lower()
        if not name or strat not in ("incremental", "merge", "replace"):
            raise SystemExit(
                f"error: {TABLES_ENV_VAR} mal formado en '{entry}' "
                "(formato Nombre:pk[:incremental|merge|replace])"
            )
        if strat in ("incremental", "merge") and not pk:
            raise SystemExit(
                f"error: {TABLES_ENV_VAR}: '{name}' con estrategia "
                f"'{strat}' requiere PK"
            )
        tables[name] = pk
        strategies[name] = strat
    if not tables:
        raise SystemExit(f"error: {TABLES_ENV_VAR} vacio")
    return tables, strategies


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

    mssql_host = _env("MSSQL_HOST", "localhost")
    mssql_port = _env("MSSQL_PORT", "1433")
    mssql_db = _env("MSSQL_DATABASE", "FAF12_Kavos")
    mssql_user = _env("MSSQL_USER", "sa")
    mssql_pw = os.getenv("MSSQL_PASSWORD") or os.getenv("MSSQL_SA_PASSWORD") or ""
    if not mssql_pw:
        raise SystemExit(
            "error: falta password MSSQL "
            "(MSSQL_PASSWORD o MSSQL_SA_PASSWORD en .env)"
        )
    # Dialecto mssql via pyodbc + ODBC 18. TrustServerCertificate=yes es
    # solo-PoC-local (cert autofirmado del contenedor); nunca en productivo.
    source_url = (
        f"mssql+pyodbc://{quote_plus(mssql_user)}:{quote_plus(mssql_pw)}"
        f"@{mssql_host}:{mssql_port}/{quote_plus(mssql_db)}"
        f"?driver={quote_plus('ODBC Driver 18 for SQL Server')}"
        f"&TrustServerCertificate=yes"
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


def check_source_types(source_url: str, tables: list[str]) -> None:
    """Falla con mensaje claro si el origen trae un tipo fuera del mapeo.

    (Requirement: tipos mapeados sin perdida; nada de truncar en silencio.)
    """
    import sqlalchemy as sa

    eng = sa.create_engine(source_url)
    with eng.connect() as conn:
        for name in tables:
            rows = conn.execute(sa.text(
                "SELECT c.name, t.name FROM sys.columns c "
                "JOIN sys.types t ON t.user_type_id = c.user_type_id "
                "WHERE c.object_id = OBJECT_ID(:q) ORDER BY c.column_id"
            ), {"q": f"{SOURCE_SCHEMA}.{name}"}).fetchall()
            if not rows:
                raise SystemExit(
                    f"error: {SOURCE_SCHEMA}.{name} no existe en MSSQL "
                    f"(MSSQL_DATABASE={_env('MSSQL_DATABASE', 'FAF12_Kavos')})"
                )
            bad = sorted({ty for _, ty in rows if ty.lower() not in SUPPORTED_TYPES})
            if bad:
                raise SystemExit(
                    f"error: {SOURCE_SCHEMA}.{name} trae tipo(s) no soportado(s): "
                    f"{bad}. Ampliar el mapeo antes de ingerir (sin truncar)."
                )


def make_resources(source_url: str, tables: list[str],
                   pks: dict[str, str], strategies: dict[str, str]):
    import dlt
    from dlt.sources.sql_database import sql_table

    resources = []
    for name in tables:
        pk = pks[name]
        strat = strategies.get(name, "incremental")
        dest_name = f"{TABLE_PREFIX}{name}"
        if strat == "replace":
            # Sin PK (p.ej. DMart_*): recarga total; converge sin duplicados
            # porque cada corrida reemplaza el contenido anterior.
            res = sql_table(
                credentials=source_url,
                schema=SOURCE_SCHEMA,
                table=name,
                write_disposition="replace",
            ).with_name(dest_name)
        elif strat == "merge":
            # Con PK pero sin cursor fiable (caso real FAF12_Kavos):
            # full-extract + upsert por PK; re-corridas convergen.
            res = sql_table(
                credentials=source_url,
                schema=SOURCE_SCHEMA,
                table=name,
                write_disposition={
                    "disposition": "merge",
                    "strategy": "upsert",
                    "primary_key": pk,
                },
            ).with_name(dest_name)
        else:
            # Legacy (DB de prueba): incremental por updated_at + merge.
            res = sql_table(
                credentials=source_url,
                schema=SOURCE_SCHEMA,
                table=name,
                incremental=dlt.sources.incremental("updated_at", initial_value=EARLY),
                write_disposition={
                    "disposition": "merge",
                    "strategy": "upsert",
                    "primary_key": pk,
                },
            ).with_name(dest_name)
        resources.append(res)
    return resources


def dest_tables(tables: list[str]) -> list[str]:
    # dlt aplica su naming convention (snake_case) al crear las tablas:
    # "mssql_BSMoneda" -> "mssql_bs_moneda". Se normaliza igual para
    # consultarlas (las tablas legacy ya-minusculas no cambian).
    from dlt.common.normalizers.naming import snake_case
    naming = snake_case.NamingConvention()
    return [naming.normalize_identifier(f"{TABLE_PREFIX}{t}") for t in tables]


def fetch_counts(pipe, tables: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with pipe.sql_client() as client:
        for t in dest_tables(tables):
            rows = client.execute_sql(f"SELECT COUNT(*) FROM bronze.{t}")
            counts[t] = int(rows[0][0])
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingesta bronze MSSQL->DuckLake (dlt)")
    tables_cfg, strategies = effective_tables()
    ap.add_argument("--tables", default=",".join(tables_cfg),
                    help="subset coma-separado (default: las configuradas)")
    ap.add_argument("--full-refresh", action="store_true",
                    help="recarga con refresh='drop_sources': dropea y resetea "
                         "SOLO las tablas/recursos mssql_* de esta corrida")
    ap.add_argument("--pipeline-name",
                    default=os.getenv("MSSQL_PIPELINE_NAME", "bronze_mssql_ingestion"))
    args = ap.parse_args(argv)

    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    unknown = [t for t in tables if t not in tables_cfg]
    if unknown:
        print(f"tablas desconocidas: {unknown} (validas: {sorted(tables_cfg)})", file=sys.stderr)
        return 2

    _, source_url, _, _ = build_urls()
    check_source_types(source_url, tables)
    pipe = make_pipeline(args.pipeline_name)

    resources = make_resources(source_url, tables, tables_cfg, strategies)
    print(f"pipeline={args.pipeline_name} dataset=bronze tablas={tables}")
    print(f"estrategias={[f'{t}={strategies[t]}' for t in tables]}")
    # NUNCA pipe.drop(): borraria el dataset `bronze` entero. refresh='drop_sources'
    # acota el drop + reset de cursor a los recursos de ESTA corrida (mssql_*).
    info = pipe.run(resources, refresh="drop_sources" if args.full_refresh else None)
    print(info)

    counts = fetch_counts(pipe, tables)
    total = sum(counts.values())
    for t in dest_tables(tables):
        print(f"bronze.{t}: {counts[t]} filas")
    print(f"TOTAL bronze (mssql): {total} filas")
    with pipe.sql_client() as client:
        for t in dest_tables(tables):
            sample = client.execute_sql(
                f"SELECT _dlt_load_id FROM bronze.{t} LIMIT 1"
            )
            print(f"bronze.{t} _dlt_load_id ejemplo: {sample[0][0] if sample else 'VACIA'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
