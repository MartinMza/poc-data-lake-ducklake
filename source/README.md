# Datos de origen simulados (`postgres-source`)

Slice `source-dataset` del change `poc-data-lake-ducklake` (tasks 3.1–3.3).
Simula el origen Aurora con datos 100% sintéticos. Sin credenciales en el
repo: la conexión va por env `SOURCE_DSN` o flag `--dsn`.

## Archivos

| Archivo | Qué es |
|---|---|
| `ddl.sql` | DDL Postgres: 4 tablas (`customers`, `units`, `leases`, `payments`) con PK/FK, `updated_at` e índices de cursor |
| `generate.py` | Generador sintético (stdlib; `psycopg` solo para `--out postgres`) |
| `requirements.txt` | Driver postgres fijado (opcional) |

## Uso

```bash
# 1. Crear esquema
psql "$SOURCE_DSN" -f source/ddl.sql

# 2. Sembrar volumen base (10k clientes + tablas derivadas)
python3 source/generate.py --mode seed --scale base

# 3. Tanda de updates (mueve updated_at -> detectable por cursor)
CURSOR=$(psql "$SOURCE_DSN" -tAc "SELECT max(updated_at) FROM payments")
python3 source/generate.py --mode update --since "$CURSOR"

# 4. Detección por cursor (lo que usará dlt en bronze)
psql "$SOURCE_DSN" -c "SELECT count(*) FROM payments WHERE updated_at > '$CURSOR'"
```

Volumen configurable: `--rows N` (clientes base) o `--scale {demo,base,10gb,50gb}`
(perfiles nominales; `10gb`/`50gb` escalan filas en proporción para el
benchmark 7.4). `--seed` fija el RNG (repetible).

Sin docker/postgres a mano: `--out sqlite` (verificación local) o `--out csv`:

```bash
python3 source/generate.py --mode seed --rows 100 --out sqlite --sqlite-file /tmp/source.db
python3 source/generate.py --mode update --out sqlite --sqlite-file /tmp/source.db --since "<cursor>"
```

## Modelo

`customers 1—N leases N—1 units`, `leases 1—N payments`. FKs `RESTRICT` (rechazan
huérfanas), `updated_at TIMESTAMPTZ` en las 4 tablas con índice (cursor
incremental). El escritor mantiene `updated_at` explícitamente (sin triggers).
