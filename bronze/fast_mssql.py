#!/usr/bin/env python3
"""Path rapido bronze MSSQL: bcp paralelo -> COPY a staging_mssql -> MERGE a bronze.

OpenSpec `mssql-fast-ingest`, tareas 2.1-2.3 + 3.1.

Sustituye el extract ODBC fila-a-fila + merge Python de dlt por:
  1. `bcp queryout` nativo en paralelo por tabla (grado configurable) con
     SELECT explicito que normaliza tipos problematicos a texto TSV:
     - money/smallmoney -> CONVERT(varchar(30), col, 2) (sin comas, exacto)
     - datetime/smalldatetime/datetime2 -> CONVERT(varchar(30), col, 126) (ISO8601)
     - date -> CONVERT(varchar(10), col, 126)
     - bit/int/... -> crudo (DuckDB parsea '0'/'1' a BOOLEAN)
     - char/varchar/nchar/nvarchar/text/ntext -> sanea CHAR(13)/CHAR(10)/CHAR(9)
       a espacio (verificado: 212 filas afectadas en el subset) y mapea el
       string vacio a CHAR(30) sentinel (verificado ausente en todo el subset;
       bcp emite NULL y '' identicos: campo vacio).
  2. `COPY` de DuckDB al esquema `staging_mssql` del MISMO lake, con
     QUOTE=CHAR(31) (verificado ausente: las comillas dobles de los datos,
     ~1700 celdas, quedan literales) y NULLSTR '' (NULL real).
     staging replica el DDL de bronze sin columnas `_dlt_*` (mismo orden y
     tipos: el MERGE es compatible por construccion).
  3. `MERGE` SQL staging -> `bronze.mssql_*` por PK (tablas merge) o
     DELETE+INSERT (tablas replace sin PK), replicando `_dlt_load_id`
     (uno por corrida) y `_dlt_id` (uuid por fila).

Ventana temporal (3.1): solo tablas con fecha fiable en TABLE_WINDOW_COL
(`BSTransaccion.FechaUltimaModificacion`; el resto va full por design: sin
fecha fiable). Predicado `fecha >= DATEADD(day, -N, GETDATE()) OR fecha IS
NULL` (los NULL se re-mergean: correcto aunque menos rapido). `--resync`
fuerza full. `--cutoff YYYY-MM-DD` fija la fecha de corte (validacion sobre
snapshots estaticos; default: GETDATE()).

No toca `pipeline.py`, `pipeline_mssql.py` (solo lo importa para reutilizar
`effective_tables`/`dest_tables`/`build_urls`), `dbt/`, `orchestration/`,
`docker-compose.yml`.

Uso:
    bronze/.venv/bin/python bronze/fast_mssql.py export [--tables ...] [--full|--resync] [--cutoff ...]
    bronze/.venv/bin/python bronze/fast_mssql.py load
    bronze/.venv/bin/python bronze/fast_mssql.py merge
    bronze/.venv/bin/python bronze/fast_mssql.py all        # export+load+merge
    bronze/.venv/bin/python bronze/fast_mssql.py parity --ref ground_truth.json
    bronze/.venv/bin/python bronze/fast_mssql.py snapshot --out ground_truth.json

Env: MSSQL_* (.env), FAST_MSSQL_PARALLEL (default nCPUs),
FAST_MSSQL_WINDOW_DAYS (default 15), FAST_MSSQL_WORKDIR (default ./mssql-fast/, gitignored).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MSSQL_TABLES_DEFAULT = (
    "BSMoneda:MonedaID:merge,BSProducto:ProductoID:merge,"
    "BSOrganizacion:OrganizacionID:merge,BSTransaccion:TransaccionID:merge,"
    "BSOperacion:TransaccionID:merge,DMart_Ventas::replace,"
    "DMart_VentasProductoCliente::replace"
)

# Solo tablas grandes con fecha fiable van por ventana (design decision 3).
TABLE_WINDOW_COL: dict[str, str] = {
    "BSTransaccion": "FechaUltimaModificacion",
}

STR_TYPES = {"char", "varchar", "nchar", "nvarchar", "text", "ntext"}


def sh(*args: str) -> str:
    out = subprocess.run(args, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"error: {' '.join(args[:6])}... falló:\n{out.stderr[-2000:]}")
    return out.stdout


def mssql_pw() -> str:
    pw = os.getenv("MSSQL_PASSWORD") or os.getenv("MSSQL_SA_PASSWORD") or ""
    if not pw and os.path.exists(".env"):
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k.strip() in ("MSSQL_PASSWORD", "MSSQL_SA_PASSWORD"):
                    pw = v.strip()
                    break
    if not pw:
        raise SystemExit("error: falta password MSSQL (MSSQL_PASSWORD/MSSQL_SA_PASSWORD)")
    return pw


def table_config() -> tuple[dict[str, str], dict[str, str]]:
    """Lee MSSQL_TABLES (default: subset PoC de 7). Import read-only de pipeline_mssql."""
    if not os.getenv("MSSQL_TABLES", "").strip():
        os.environ["MSSQL_TABLES"] = MSSQL_TABLES_DEFAULT
    from pipeline_mssql import effective_tables

    return effective_tables()


def source_columns(table: str) -> list[tuple[str, str]]:
    """[(nombre, tipo)] en orden de column_id desde sys.columns."""
    from pipeline_mssql import build_urls

    import sqlalchemy as sa

    _, source_url, _, _ = build_urls()
    eng = sa.create_engine(source_url)
    with eng.connect() as conn:
        db = os.getenv("MSSQL_DATABASE", "FAF12_Kavos")
        conn.execute(sa.text(f"USE [{db}]"))
        rows = conn.execute(
            sa.text(
                "SELECT c.name, t.name FROM sys.columns c "
                "JOIN sys.types t ON t.user_type_id = c.user_type_id "
                "WHERE c.object_id = OBJECT_ID(:q) ORDER BY c.column_id"
            ),
            {"q": f"dbo.{table}"},
        ).fetchall()
    if not rows:
        raise SystemExit(f"error: dbo.{table} no existe en MSSQL_DATABASE")
    return [(c, ty.lower()) for c, ty in rows]


def export_settings(args) -> tuple[int | None, str, int]:
    window_days = None if args.full else int(os.getenv("FAST_MSSQL_WINDOW_DAYS", "15"))
    if args.window_days is not None:
        window_days = args.window_days
    if args.resync:
        window_days = None
    try:
        parallel = int(os.getenv("FAST_MSSQL_PARALLEL", "") or os.cpu_count() or 4)
    except ValueError:
        parallel = 4
    return window_days, args.cutoff, parallel


def export_one(table: str, window_days: int | None, cutoff: str,
               host: str, user: str, pw: str, workdir: str) -> float:
    q = build_query_raw(table, window_days, cutoff or "")
    t0 = time.time()
    sh("docker", "compose", "exec", "-T", "mssql",
       "/opt/mssql-tools18/bin/bcp", q, "queryout",
       f"/tmp/fast_mssql/{table}.tsv",
       "-S", host, "-U", user, "-P", pw,
       "-u", "-C", "65001", "-c", "-t", "\t")
    sh("docker", "compose", "cp", f"mssql:/tmp/fast_mssql/{table}.tsv",
       f"{workdir}/{table}.tsv")
    dt = time.time() - t0
    mode = "FULL" if (window_days is None or table not in TABLE_WINDOW_COL) else f"WIN{window_days}d"
    print(f"export {table} [{mode}]: {dt:.1f}s", flush=True)
    return dt


def cmd_export(args) -> int:
    tables_cfg, _ = table_config()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else list(tables_cfg)
    workdir = os.getenv("FAST_MSSQL_WORKDIR", "mssql-fast")
    os.makedirs(workdir, exist_ok=True)
    host = os.getenv("MSSQL_HOST", "localhost")
    user = os.getenv("MSSQL_USER", "sa")
    pw = mssql_pw()
    window_days, cutoff, parallel = export_settings(args)

    sh("docker", "compose", "exec", "-T", "mssql", "mkdir", "-p", "/tmp/fast_mssql")
    timings: dict[str, float] = {}

    def one(table: str) -> str:
        timings[table] = export_one(table, window_days, cutoff, host, user, pw, workdir)
        return table

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(parallel, len(tables))) as ex:
        list(ex.map(one, tables))
    total = time.time() - t0
    meta = {"mode": {t: ("FULL" if (window_days is None or t not in TABLE_WINDOW_COL) else f"WIN{window_days}d") for t in tables},
            "window_days": window_days, "cutoff": args.cutoff, "seconds": timings, "total": total}
    with open(f"{workdir}/export_meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    print(f"EXPORT total: {total:.1f}s (paralelismo={min(parallel, len(tables))})")
    return 0


def dirty_str_cols(table: str) -> set[str]:
    """Columnas string con TAB/LF/CR (las unicas que necesitan REPLACE).

    El scan es un full-scan barato (LIKE sin funcion por fila salvo el propio
    match) y deja el export en ~2s en vez de ~10s en tablas grandes.
    """
    from pipeline_mssql import build_urls

    import sqlalchemy as sa

    _, source_url, _, _ = build_urls()
    cols = source_columns(table)
    scols = [c for c, t in cols if t in STR_TYPES]
    if not scols:
        return set()
    eng = sa.create_engine(source_url)
    db = os.getenv("MSSQL_DATABASE", "FAF12_Kavos")
    # Un solo full-scan con un CASE por columna (N queries secuenciales
    # full-scan c/u anulaban la ganancia en tablas anchas como BSOperacion).
    exprs = []
    for c in scols:
        e = f"CAST([{c}] AS nvarchar(max))"
        # Un solo LIKE con clase [...] en vez de 3 ORs (3x menos evaluaciones).
        exprs.append(
            f"SUM(CASE WHEN {e} LIKE '%['+CHAR(9)+CHAR(10)+CHAR(13)+']%' "
            f"THEN 1 ELSE 0 END)")
    dirty: set[str] = set()
    with eng.connect() as conn:
        conn.execute(sa.text(f"USE [{db}]"))
        row = conn.execute(sa.text(
            f"SELECT {', '.join(exprs)} FROM dbo.[{table}]")).fetchone()
        assert row is not None
        dirty = {c for c, n in zip(scols, row) if (n or 0) > 0}
    return dirty


def build_query_raw(table: str, window_days: int | None, cutoff: str | None) -> str:
    """SELECT posicional para bcp queryout (bcp no necesita alias)."""
    cols = source_columns(table)
    dirty = dirty_str_cols(table)
    parts = []
    for c, t in cols:
        q = f"[{c}]"
        if t in ("bigint", "int", "smallint", "tinyint", "bit", "decimal", "numeric"):
            parts.append(q)
        elif t in ("money", "smallmoney"):
            parts.append(f"CONVERT(varchar(30), {q}, 2)")
        elif t in ("datetime", "smalldatetime", "datetime2"):
            parts.append(f"CONVERT(varchar(30), {q}, 126)")
        elif t == "date":
            parts.append(f"CONVERT(varchar(10), {q}, 126)")
        elif t == "time":
            parts.append(f"CONVERT(varchar(16), {q}, 114)")
        elif t == "uniqueidentifier":
            parts.append(f"CONVERT(varchar(36), {q})")
        elif t in STR_TYPES:
            e = f"CAST({q} AS nvarchar(max))"
            if c in dirty:
                sanit = (f"REPLACE(REPLACE(REPLACE({e}, CHAR(13), ' '), CHAR(10), ' '), CHAR(9), ' ')")
            else:
                sanit = e
            # OJO: en T-SQL `' ' = ''` es TRUE (espacios finales ignorados);
            # solo DATALENGTH distingue '' (0) de ' ' (>=1) como hace dlt.
            parts.append(f"CASE WHEN DATALENGTH({e}) = 0 THEN CHAR(30) ELSE {sanit} END")
        else:
            raise SystemExit(f"error: tipo no soportado en export bcp: {c} {t}")
    where = ""
    wcol = TABLE_WINDOW_COL.get(table)
    if window_days is not None and wcol:
        if cutoff:
            where = f" WHERE ([{wcol}] >= CONVERT(datetime, '{cutoff}') OR [{wcol}] IS NULL)"
        else:
            where = f" WHERE ([{wcol}] >= DATEADD(day, -{window_days}, GETDATE()) OR [{wcol}] IS NULL)"
    db = os.getenv("MSSQL_DATABASE", "FAF12_Kavos")
    # bcp conecta a master por defecto: nombre a 3 partes.
    return f"SELECT {', '.join(parts)} FROM [{db}].dbo.[{table}]{where}"


def lake():
    import lake_counts

    con = lake_counts.connect()
    con.execute("USE lake")
    return con


def staging_ddl(tables: list[str]) -> dict[str, str]:
    """DDL de staging por tabla origen: tipos bronze en orden de sys.columns.

    bcp emite posicional en orden de origen pero dlt reordena (p.ej. MONEY al
    final); el mapeo de nombres va via normalizer dlt.
    """
    from dlt.common.normalizers.naming import snake_case as _sc

    from pipeline_mssql import dest_tables

    naming = _sc.NamingConvention()
    con0 = lake()
    con0.execute("CREATE SCHEMA IF NOT EXISTS staging_mssql")
    out: dict[str, str] = {}
    for src, dst in zip(tables, dest_tables(tables)):
        btypes = {r[0]: r[1] for r in con0.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema='bronze' AND table_name='{dst}' "
            "AND column_name NOT LIKE '\\_dlt\\_%' ESCAPE '\\'").fetchall()}
        ordered = []
        for c, _ in source_columns(src):
            n = naming.normalize_identifier(c)
            if n not in btypes:
                raise SystemExit(f"error: staging {src}.{c} -> '{n}' sin columna bronze")
            ordered.append((n, btypes[n]))
        if sorted(n for n, _ in ordered) != sorted(btypes):
            raise SystemExit(f"error: staging {src}: columnas no coinciden con bronze.{dst}")
        out[src] = ", ".join(f'"{c}" {t}' for c, t in ordered)
    return out


def load_one(src: str, dst: str, ddl: str, workdir: str) -> None:
    con = lake()
    con.execute(f"CREATE OR REPLACE TABLE staging_mssql.{dst} ({ddl})")
    path = os.path.abspath(f"{workdir}/{src}.tsv")
    con.execute(
        f"COPY staging_mssql.{dst} FROM '{path}' "
        "(DELIMITER '\t', NULLSTR '', QUOTE '\x1f', HEADER false)")
    n = con.execute(f"SELECT COUNT(*) FROM staging_mssql.{dst}").fetchone()[0]
    # Cada fila origen es exactamente una linea (el export sanea \t\n\r);
    # si COPY salta o parte lineas, se aborta aca, no en paridad.
    with open(path, "rb") as fh:
        lines = sum(1 for _ in fh)
    if n != lines:
        raise SystemExit(
            f"error: staging_mssql.{dst}: COPY cargo {n} filas pero el TSV "
            f"tiene {lines} lineas")
    print(f"load staging_mssql.{dst}: {n} filas", flush=True)


def merge_one(src: str, dst: str, strat: str, pk: str, load_id: str) -> None:
    con = lake()
    cols = [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema='bronze' AND table_name='{dst}' "
        "AND column_name NOT LIKE '\\_dlt\\_%' ESCAPE '\\' "
        "ORDER BY ordinal_position").fetchall()]
    str_cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema='bronze' AND table_name='{dst}' "
        "AND data_type='VARCHAR'").fetchall()}
    proj = ", ".join(
        f"(CASE WHEN \"{c}\" = CHR(30) THEN '' ELSE \"{c}\" END) AS \"{c}\""
        if c in str_cols else f"\"{c}\"" for c in cols)
    if strat == "replace":
        con.execute(f"DELETE FROM bronze.{dst}")
        con.execute(
            f"INSERT INTO bronze.{dst} ({', '.join(chr(34) + c + chr(34) for c in cols)}, "
            f"_dlt_load_id, _dlt_id) SELECT {proj}, '{load_id}', uuid()::VARCHAR "
            f"FROM staging_mssql.{dst}")
    else:
        set_clause = ", ".join(
            f'"{c}" = s."{c}"' for c in cols if c != pk)
        con.execute(
            f"MERGE INTO bronze.{dst} b USING "
            f"(SELECT {proj}, '{load_id}' AS _dlt_load_id, uuid()::VARCHAR AS _dlt_id "
            f"FROM staging_mssql.{dst}) s ON b.\"{pk}\" = s.\"{pk}\" "
            f"WHEN MATCHED THEN UPDATE SET {set_clause}, "
            f"_dlt_load_id = s._dlt_load_id, _dlt_id = s._dlt_id "
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(chr(34) + c + chr(34) for c in cols)}, "
            f"_dlt_load_id, _dlt_id) VALUES ({', '.join('s.' + chr(34) + c + chr(34) for c in cols)}, "
            f"s._dlt_load_id, s._dlt_id)")
    n = con.execute(f"SELECT COUNT(*) FROM bronze.{dst}").fetchone()[0]
    print(f"merge bronze.{dst} [{strat}]: {n} filas (load_id={load_id})", flush=True)


def cmd_load(args) -> int:
    from pipeline_mssql import dest_tables

    tables_cfg, _ = table_config()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else list(tables_cfg)
    workdir = os.getenv("FAST_MSSQL_WORKDIR", "mssql-fast")
    ddl_done = staging_ddl(tables)
    t0 = time.time()

    def one_load(pair: tuple[str, str]) -> None:
        src, dst = pair
        load_one(src, dst, ddl_done[src], workdir)

    with ThreadPoolExecutor(max_workers=min(4, len(tables))) as ex:
        list(ex.map(one_load, list(zip(tables, dest_tables(tables)))))
    print(f"LOAD total: {time.time() - t0:.1f}s")
    return 0


def cmd_merge(args) -> int:
    from dlt.common.normalizers.naming import snake_case

    from pipeline_mssql import dest_tables

    naming = snake_case.NamingConvention()
    tables_cfg, strategies = table_config()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else list(tables_cfg)
    load_id = os.getenv("FAST_MSSQL_LOAD_ID") or f"fast-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    t0 = time.time()

    def one_merge(pair: tuple[str, str]) -> None:
        src, dst = pair
        pk = naming.normalize_identifier(tables_cfg[src]) if strategies[src] != "replace" else ""
        merge_one(src, dst, strategies[src], pk, load_id)

    with ThreadPoolExecutor(max_workers=min(4, len(tables))) as ex:
        list(ex.map(one_merge, list(zip(tables, dest_tables(tables)))))
    print(f"MERGE total: {time.time() - t0:.1f}s")
    return 0


def cmd_all(args) -> int:
    """Pipeline encadenado por tabla: export->load->merge en un solo hilo
    por tabla, tablas en paralelo. El wall pasa a ser la tabla mas lenta."""
    from dlt.common.normalizers.naming import snake_case

    from pipeline_mssql import dest_tables

    naming = snake_case.NamingConvention()
    tables_cfg, strategies = table_config()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else list(tables_cfg)
    workdir = os.getenv("FAST_MSSQL_WORKDIR", "mssql-fast")
    os.makedirs(workdir, exist_ok=True)
    host = os.getenv("MSSQL_HOST", "localhost")
    user = os.getenv("MSSQL_USER", "sa")
    pw = mssql_pw()
    window_days, cutoff, parallel = export_settings(args)
    sh("docker", "compose", "exec", "-T", "mssql", "mkdir", "-p", "/tmp/fast_mssql")
    ddl_done = staging_ddl(tables)
    dst_of = dict(zip(tables, dest_tables(tables)))
    load_id = f"fast-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    t0 = time.time()

    def chain(table: str) -> None:
        export_one(table, window_days, cutoff, host, user, pw, workdir)
        load_one(table, dst_of[table], ddl_done[table], workdir)
        pk = naming.normalize_identifier(tables_cfg[table]) if strategies[table] != "replace" else ""
        merge_one(table, dst_of[table], strategies[table], pk, load_id)

    with ThreadPoolExecutor(max_workers=min(parallel, len(tables))) as ex:
        list(ex.map(chain, tables))
    total = time.time() - t0
    print(f"ALL total: {total:.1f}s (paralelismo={min(parallel, len(tables))})")
    return 0


def checksums(con, schema: str, tables: list[str]) -> dict:
    """Conteos + nulos por col + sumas numericas + vacios en strings."""
    from pipeline_mssql import dest_tables

    out: dict = {}
    for src, dst in zip(tables, dest_tables(tables)):
        cols = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_schema='{schema}' AND table_name='{dst}' "
            "AND column_name NOT LIKE '\\_dlt\\_%' ESCAPE '\\' "
            "ORDER BY ordinal_position").fetchall()
        t: dict = {"count": con.execute(f"SELECT COUNT(*) FROM {schema}.{dst}").fetchone()[0]}
        for c, dt_ in cols:
            nulls = con.execute(f'SELECT COUNT(*) FROM {schema}.{dst} WHERE "{c}" IS NULL').fetchone()[0]
            t[f"null:{c}"] = nulls
            if dt_.startswith(("DECIMAL", "NUMERIC", "BIGINT", "INTEGER", "SMALLINT", "DOUBLE")):
                s = con.execute(f'SELECT SUM("{c}"::DECIMAL(38,9)) FROM {schema}.{dst}').fetchone()[0]
                t[f"sum:{c}"] = str(s)
            elif dt_ == "VARCHAR":
                e = con.execute(f"SELECT COUNT(*) FROM {schema}.{dst} WHERE \"{c}\" = ''").fetchone()[0]
                t[f"empty:{c}"] = e
        out[dst] = t
    return out


def cmd_snapshot(args) -> int:
    tables_cfg, _ = table_config()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else list(tables_cfg)
    con = lake()
    snap = checksums(con, "bronze", tables)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=1)
    print(f"snapshot legacy -> {args.out} ({len(tables)} tablas)")
    return 0


def cmd_parity(args) -> int:
    tables_cfg, _ = table_config()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()] if args.tables else list(tables_cfg)
    with open(args.ref, encoding="utf-8") as fh:
        ref = json.load(fh)
    con = lake()
    cur = checksums(con, "bronze", tables)
    from pipeline_mssql import dest_tables as _dt
    wanted = set(_dt(tables))
    ok = True
    for dst in ref:
        if dst not in wanted:
            continue
        r, c = ref[dst], cur.get(dst, {})
        if r != c:
            ok = False
            print(f"DIVERGE {dst}:")
            for k in r:
                if r.get(k) != c.get(k):
                    print(f"  {k}: legacy={r.get(k)} fast={c.get(k)}")
        else:
            nkeys = len(r)
            print(f"OK {dst}: {r['count']} filas, {nkeys} checks iguales")
    print("PARIDAD TOTAL" if ok else "PARIDAD FALLIDA")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Path rapido MSSQL (bcp -> staging -> MERGE)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("export", "load", "merge", "all"):
        p = sub.add_parser(name)
        p.add_argument("--tables", default="")
        p.add_argument("--full", action="store_true", help="export full aunque haya ventana")
        p.add_argument("--resync", action="store_true", help="alias de --full (3.1)")
        p.add_argument("--cutoff", default="", help="YYYY-MM-DD (validacion; default GETDATE()-N)")
        p.add_argument("--window-days", type=int, default=None)
    p = sub.add_parser("snapshot")
    p.add_argument("--tables", default="")
    p.add_argument("--out", default="ground_truth.json")
    p = sub.add_parser("parity")
    p.add_argument("--tables", default="")
    p.add_argument("--ref", default="ground_truth.json")
    args = ap.parse_args(argv)
    if args.cmd == "export":
        return cmd_export(args)
    if args.cmd == "load":
        return cmd_load(args)
    if args.cmd == "merge":
        return cmd_merge(args)
    if args.cmd == "all":
        return cmd_all(args)
    if args.cmd == "snapshot":
        return cmd_snapshot(args)
    if args.cmd == "parity":
        return cmd_parity(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
