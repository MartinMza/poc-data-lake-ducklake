#!/usr/bin/env python3
"""Generador sintético para `postgres-source` (PoC Data Lake / DuckLake).

Tasks 3.2–3.3 del change `poc-data-lake-ducklake`. Solo datos sintéticos,
sin credenciales reales: la conexión se pasa por --dsn o env SOURCE_DSN.

Modos:
  seed    puebla (o repuebla) las 4 tablas con volumen configurable.
  update  aplica una tanda de updates/inserts que mueve `updated_at`
          (demonstrable con `WHERE updated_at > :cursor`).

Volumen:
  --rows N            nº de clientes base (las demás tablas escalan en
                      proporción; ver _derive_counts).
  --scale {demo,base,10gb,50gb}
                      perfiles nominales. `demo` es el perfil por defecto
                      (liviano, para CI/local). `10gb`/`50gb` son
                      extrapolaciones para el benchmark (tarea 7.4): el
                      generador los acepta y escala filas en proporción,
                      pero materializarlos requiere el host del benchmark.

Salidas (--out):
  postgres  escribe en postgres-source vía psycopg (requiere
            `pip install -r source/requirements.txt`).
  csv       un CSV por tabla en --out-dir (auditoría / carga manual).
  sqlite    base SQLite en --sqlite-file (verificación local sin docker).

Ejemplos:
  python3 source/generate.py --mode seed --rows 100 --out sqlite
  python3 source/generate.py --mode update --out sqlite --since "$CURSOR"
  SOURCE_DSN=postgresql://user:pass@localhost:5432/source \\
      python3 source/generate.py --mode seed --scale base

Solo stdlib salvo el driver postgres (import perezoso).
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

TABLES = ("customers", "units", "leases", "payments")

# Perfiles nominales: nº de clientes base. El resto deriva en proporción.
PROFILES = {
    "demo": 100,
    "base": 10_000,
    "10gb": 2_000_000,
    "50gb": 10_000_000,
}

FIRST = ["Ana", "Bruno", "Carla", "Diego", "Elena", "Facundo", "Gabi",
         "Hugo", "Inés", "Juan", "Karen", "Lucía", "Martín", "Nadia",
         "Oscar", "Paula", "Ramiro", "Sofía", "Tomás", "Valeria"]
LAST = ["García", "Rodríguez", "Martínez", "Fernández", "López", "Pérez",
        "Sánchez", "Ramírez", "Torres", "Vargas", "Castillo", "Morales",
        "Reyes", "Gutiérrez", "Ortiz", "Chávez", "Ruiz", "Díaz", "Méndez"]
CITIES = ["Montevideo", "Salto", "Paysandú", "Maldonado", "Colonia",
          "Canelones", "Rivera", "Tacuarembó"]
STATUSES = ["active", "active", "active", "closed", "cancelled"]

EPOCH = date(2023, 1, 1)


def _derive_counts(n_customers: int) -> dict[str, int]:
    """Deriva el volumen por tabla a partir del nº de clientes."""
    n_units = max(10, n_customers // 2)
    n_leases = n_customers * 2
    n_payments = n_leases * 6
    return {"customers": n_customers, "units": n_units,
            "leases": n_leases, "payments": n_payments}


def _ts(rng: random.Random, lo: date, hi: date) -> datetime:
    span = max(1, (hi - lo).days)
    return datetime.combine(
        lo + timedelta(days=rng.randrange(span)), datetime.min.time(),
        tzinfo=timezone.utc) + timedelta(seconds=rng.randrange(86_400))


def generate(rng: random.Random, counts: dict[str, int]) -> dict[str, list[dict]]:
    """Genera filas respetando FKs (orden: customers → units → leases → payments)."""
    today = date(2026, 10, 8)
    data: dict[str, list[dict]] = {t: [] for t in TABLES}

    for i in range(1, counts["customers"] + 1):
        fn, ln = rng.choice(FIRST), rng.choice(LAST)
        created = _ts(rng, EPOCH, today)
        data["customers"].append({
            "customer_id": i,
            "full_name": f"{fn} {ln}",
            "email": f"{fn.lower()}.{ln.lower()}.{i}@example.test",
            "country": "UY",
            "created_at": created.isoformat(),
            "updated_at": created.isoformat(),
        })

    for i in range(1, counts["units"] + 1):
        created = _ts(rng, EPOCH, today)
        data["units"].append({
            "unit_id": i,
            "code": f"U-{i:06d}",
            "city": rng.choice(CITIES),
            "monthly_rent": round(rng.uniform(15_000, 120_000), 2),
            "created_at": created.isoformat(),
            "updated_at": created.isoformat(),
        })

    for i in range(1, counts["leases"] + 1):
        start = EPOCH + timedelta(days=rng.randrange((today - EPOCH).days))
        status = rng.choice(STATUSES)
        end = (start + timedelta(days=rng.randrange(180, 900))
               if status != "active" else None)
        created = _ts(rng, start, today)
        data["leases"].append({
            "lease_id": i,
            "customer_id": rng.randint(1, counts["customers"]),
            "unit_id": rng.randint(1, counts["units"]),
            "start_date": start.isoformat(),
            "end_date": end.isoformat() if end else None,
            "status": status,
            "created_at": created.isoformat(),
            "updated_at": created.isoformat(),
        })

    pid = 0
    for lease in data["leases"]:
        for _ in range(rng.randint(3, 9)):
            pid += 1
            paid = _ts(rng, date.fromisoformat(lease["start_date"]), today)
            data["payments"].append({
                "payment_id": pid,
                "lease_id": lease["lease_id"],
                "amount": round(rng.uniform(15_000, 120_000), 2),
                "currency": rng.choice(["UYU", "UYU", "UYU", "USD"]),
                "paid_at": paid.isoformat(),
                "created_at": paid.isoformat(),
                "updated_at": paid.isoformat(),
            })
    return data


# --------------------------------------------------------------------------
# Escritores
# --------------------------------------------------------------------------

def write_csv(data: dict[str, list[dict]], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for table, rows in data.items():
        path = os.path.join(out_dir, f"{table}.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"{table}: {len(rows)} filas -> {path}")


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (customer_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE, country TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS units (unit_id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE,
  city TEXT NOT NULL, monthly_rent REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases (lease_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL
  REFERENCES customers(customer_id), unit_id INTEGER NOT NULL REFERENCES units(unit_id),
  start_date TEXT NOT NULL, end_date TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments (payment_id INTEGER PRIMARY KEY, lease_id INTEGER NOT NULL
  REFERENCES leases(lease_id), amount REAL NOT NULL, currency TEXT NOT NULL,
  paid_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_leases_updated_at ON leases (updated_at);
CREATE INDEX IF NOT EXISTS ix_payments_updated_at ON payments (updated_at);
CREATE INDEX IF NOT EXISTS ix_customers_updated_at ON customers (updated_at);
CREATE INDEX IF NOT EXISTS ix_units_updated_at ON units (updated_at);
"""


def write_sqlite(data: dict[str, list[dict]], path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript(_SQLITE_SCHEMA)
    for table, rows in data.items():
        cols = list(rows[0].keys())
        con.executemany(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [[r[c] for c in cols] for r in rows])
    con.commit()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
    con.close()
    for table in TABLES:
        print(f"{table}: {counts[table]} filas -> {path}")


def write_postgres(data: dict[str, list[dict]], dsn: str) -> None:
    try:
        import psycopg  # type: ignore
    except ImportError:
        sys.exit("psycopg no instalado: pip install -r source/requirements.txt")
    ddl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ddl.sql")
    with open(ddl_path, encoding="utf-8") as fh:
        ddl = fh.read()
    with psycopg.connect(dsn) as con:
        con.execute(ddl)
        con.execute("TRUNCATE customers, units, leases, payments RESTART IDENTITY CASCADE")
        for table, rows in data.items():
            cols = list(rows[0].keys())
            with con.cursor() as cur:
                # IDs explícitos sobre GENERATED ALWAYS AS IDENTITY exigen
                # OVERRIDING SYSTEM VALUE (si no: psycopg.errors.GeneratedAlways).
                cur.executemany(
                    f"INSERT INTO {table} ({','.join(cols)}) "
                    f"OVERRIDING SYSTEM VALUE "
                    f"VALUES ({','.join(['%s'] * len(cols))})",
                    [[r[c] for c in cols] for r in rows])
        # Las secuencias quedaron atrás del max(id) explícito: avanzarlas o
        # los INSERT sin ID (p. ej. --mode update) colisionan en 1.
        for table, pk in (("customers", "customer_id"), ("units", "unit_id"),
                          ("leases", "lease_id"), ("payments", "payment_id")):
            con.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{pk}'), "
                f"(SELECT max({pk}) FROM {table}))")
        con.commit()
    for table, rows in data.items():
        print(f"{table}: {len(rows)} filas insertadas (postgres)")


# --------------------------------------------------------------------------
# Update: mueve updated_at y agrega pagos (cursor incremental)
# --------------------------------------------------------------------------

def apply_update_sqlite(path: str, rng: random.Random, fraction: float,
                        new_payments: int, since: str | None) -> None:
    con = sqlite3.connect(path)
    now = datetime.now(timezone.utc).isoformat()
    n_leases = con.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
    n_pay = con.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    touch_leases = max(1, int(n_leases * fraction))
    touch_pay = max(1, int(n_pay * fraction))
    con.execute(
        "UPDATE leases SET status='closed', updated_at=? "
        "WHERE lease_id IN (SELECT lease_id FROM leases ORDER BY RANDOM() LIMIT ?)",
        (now, touch_leases))
    con.execute(
        "UPDATE payments SET updated_at=? WHERE payment_id IN "
        "(SELECT payment_id FROM payments ORDER BY RANDOM() LIMIT ?)",
        (now, touch_pay))
    max_pid = con.execute("SELECT MAX(payment_id) FROM payments").fetchone()[0] or 0
    max_lease = con.execute("SELECT MAX(lease_id) FROM leases").fetchone()[0] or 1
    for k in range(1, new_payments + 1):
        con.execute(
            "INSERT INTO payments (payment_id, lease_id, amount, currency, paid_at,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (max_pid + k, rng.randint(1, max_lease),
             round(rng.uniform(15_000, 120_000), 2), "UYU", now, now, now))
    con.commit()
    print(f"leases tocados: {touch_leases}, payments tocados: {touch_pay}, "
          f"payments nuevos: {new_payments}")
    if since:
        report_cursor(con, since)
    con.close()


def apply_update_postgres(dsn: str, fraction: float, new_payments: int,
                          since: str | None) -> None:
    try:
        import psycopg  # type: ignore
    except ImportError:
        sys.exit("psycopg no instalado: pip install -r source/requirements.txt")
    with psycopg.connect(dsn) as con:
        n_leases = con.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        n_pay = con.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        touch_leases = max(1, int(n_leases * fraction))
        touch_pay = max(1, int(n_pay * fraction))
        con.execute(
            "UPDATE leases SET status='closed', updated_at=now() "
            "WHERE lease_id IN (SELECT lease_id FROM leases ORDER BY random() LIMIT %s)",
            (touch_leases,))
        con.execute(
            "UPDATE payments SET updated_at=now() WHERE payment_id IN "
            "(SELECT payment_id FROM payments ORDER BY random() LIMIT %s)",
            (touch_pay,))
        con.execute(
            "INSERT INTO payments (lease_id, amount, currency, paid_at, updated_at) "
            "SELECT (SELECT lease_id FROM leases ORDER BY random() LIMIT 1), "
            " (random()*105000+15000)::numeric(12,2), 'UYU', now(), now() "
            "FROM generate_series(1, %s)", (new_payments,))
        con.commit()
        print(f"leases tocados: {touch_leases}, payments tocados: {touch_pay}, "
              f"payments nuevos: {new_payments}")
        if since:
            for (table,) in (("customers",), ("units",), ("leases",), ("payments",)):
                n = con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE updated_at > %s",
                    (since,)).fetchone()[0]
                print(f"cursor {table}: {n} filas con updated_at > {since}")


def report_cursor(con: sqlite3.Connection, since: str) -> None:
    for table in TABLES:
        n = con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE updated_at > ?", (since,)).fetchone()[0]
        print(f"cursor {table}: {n} filas con updated_at > {since}")


# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Generador sintético de postgres-source")
    ap.add_argument("--mode", choices=("seed", "update"), default="seed")
    ap.add_argument("--rows", type=int, default=None,
                    help="nº de clientes base (deriva el resto de tablas)")
    ap.add_argument("--scale", choices=sorted(PROFILES), default=None,
                    help="perfil nominal (default: demo salvo que --rows indique)")
    ap.add_argument("--seed", type=int, default=42, help="semilla RNG (repetible)")
    ap.add_argument("--out", choices=("postgres", "csv", "sqlite"), default="sqlite")
    ap.add_argument("--dsn", default=os.getenv("SOURCE_DSN", ""),
                    help="DSN postgres (o env SOURCE_DSN)")
    ap.add_argument("--out-dir", default="source/_data",
                    help="destino CSV (solo --out csv)")
    ap.add_argument("--sqlite-file", default="source/_data/source.db",
                    help="fichero SQLite (solo --out sqlite)")
    ap.add_argument("--update-fraction", type=float, default=0.05,
                    help="fracción de leases/payments que toca --mode update")
    ap.add_argument("--new-payments", type=int, default=None,
                    help="pagos nuevos en update (default: 10%% de payments)")
    ap.add_argument("--since", default=None,
                    help="cursor ISO; tras update reporta filas con updated_at > since")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    rng = random.Random(args.seed)

    if args.mode == "seed":
        n = args.rows if args.rows is not None else PROFILES[args.scale or "demo"]
        counts = _derive_counts(n)
        profile = args.scale or ("custom" if args.rows else "demo")
        print(f"perfil={profile} seed={args.seed} counts={counts}")
        data = generate(rng, counts)
        if args.out == "csv":
            write_csv(data, args.out_dir)
        elif args.out == "sqlite":
            os.makedirs(os.path.dirname(args.sqlite_file) or ".", exist_ok=True)
            write_sqlite(data, args.sqlite_file)
        else:
            if not args.dsn:
                sys.exit("falta --dsn o env SOURCE_DSN para --out postgres")
            write_postgres(data, args.dsn)
        return

    # --mode update (requiere datos existentes)
    if args.out == "sqlite":
        if not os.path.exists(args.sqlite_file):
            sys.exit(f"no existe {args.sqlite_file}; correr --mode seed primero")
        con = sqlite3.connect(args.sqlite_file)
        n_pay = con.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        con.close()
        new_pay = args.new_payments if args.new_payments is not None else max(1, n_pay // 10)
        apply_update_sqlite(args.sqlite_file, rng, args.update_fraction, new_pay, args.since)
    elif args.out == "postgres":
        if not args.dsn:
            sys.exit("falta --dsn o env SOURCE_DSN para --out postgres")
        try:
            import psycopg  # type: ignore
            with psycopg.connect(args.dsn) as con:
                n_pay = con.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        except ImportError:
            sys.exit("psycopg no instalado: pip install -r source/requirements.txt")
        new_pay = args.new_payments if args.new_payments is not None else max(1, n_pay // 10)
        apply_update_postgres(args.dsn, args.update_fraction, new_pay, args.since)
    else:
        sys.exit("--mode update solo soporta --out sqlite|postgres (csv es snapshot)")


if __name__ == "__main__":
    main()
