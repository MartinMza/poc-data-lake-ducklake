#!/usr/bin/env bash
# Restore del lakehouse (OpenSpec poc-data-lake-ducklake, tarea 7.1).
#
# Desde <BACKUP_DIR> (ver scripts/backup.sh):
#   1. recrea la base del catálogo y aplica pg_restore (metadata DuckLake)
#   2. purga el bucket y re-sube el espejo (Parquet del lago)
#   3. compara conteos actuales vs counts.json del backup (converge o FAIL)
#
# Uso: ./scripts/restore.sh <backup_dir>
# Sin escritores concurrentes: el restore reemplaza catálogo + objetos.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

CATALOG_DB="${CATALOG_DB:-ducklake_catalog}"
CATALOG_USER="${CATALOG_USER:-ducklake}"
LAKEHOUSE_BUCKET="${LAKEHOUSE_BUCKET:-lakehouse}"
BRONZE_PYTHON="${BRONZE_PYTHON:-bronze/.venv/bin/python}"
SRC="${1:?uso: $0 <backup_dir>}"

for f in catalog.dump counts.json manifest.json; do
  [[ -f "$SRC/$f" ]] || { echo "falta $SRC/$f" >&2; exit 1; }
done
echo "$SRC/catalog.dump" | grep -q . # existe (sha se verifica abajo)
echo "$(cat "$SRC/catalog.dump.sha256")  $SRC/catalog.dump" | sha256sum -c -

echo "==> [1/3] recreate catálogo $CATALOG_DB + pg_restore"
docker compose exec -T postgres-catalog psql -U "$CATALOG_USER" -d postgres \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$CATALOG_DB' AND pid <> pg_backend_pid();" \
  > /dev/null
docker compose exec -T postgres-catalog psql -U "$CATALOG_USER" -d postgres \
  -c "DROP DATABASE IF EXISTS \"$CATALOG_DB\";" \
  -c "CREATE DATABASE \"$CATALOG_DB\";"
docker compose exec -T postgres-catalog pg_restore \
  -U "$CATALOG_USER" -d "$CATALOG_DB" --no-owner --no-privileges \
  < "$SRC/catalog.dump" > /dev/null
echo "    pg_restore OK"

echo "==> [2/3] purge + re-subida del bucket s3://$LAKEHOUSE_BUCKET/"
DULSE_ALLOW_PURGE=yes "$BRONZE_PYTHON" scripts/s3_sync.py purge \
  "s3://$LAKEHOUSE_BUCKET/"
"$BRONZE_PYTHON" scripts/s3_sync.py upload \
  "$SRC/bucket/" "s3://$LAKEHOUSE_BUCKET/"

echo "==> [3/3] verificación de convergencia vs counts.json"
"$BRONZE_PYTHON" scripts/lake_counts.py > "$SRC/counts.after.json"
"$BRONZE_PYTHON" - "$SRC/counts.json" "$SRC/counts.after.json" <<'EOF'
import json, sys
before = json.load(open(sys.argv[1]))
after = json.load(open(sys.argv[2]))
ok = True
for k in sorted(set(before) | set(after)):
    b, a = before.get(k), after.get(k)
    mark = "OK " if b == a else "DIF"
    if b != a:
        ok = False
    print(f"[{mark}] {k}: backup={b} restore={a}")
sys.exit(0 if ok else 1)
EOF
echo "restore OK: conteos idénticos al backup"
