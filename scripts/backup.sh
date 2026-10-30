#!/usr/bin/env bash
# Backup del lakehouse (OpenSpec poc-data-lake-ducklake, tarea 7.1).
#
# Guarda en <BACKUP_DIR> (default backups/<timestamp>):
#   catalog.dump   dump custom (-Fc) de postgres-catalog (metadata DuckLake)
#   catalog.dump.sha256
#   bucket/        espejo completo del bucket Garage (Parquet del lago)
#   counts.json    conteos bronze/silver/gold (línea base para el restore)
#   manifest.json  fecha, versiones, tamaños
#
# Uso: ./scripts/backup.sh [destino]
# Requiere: stack levantado, bronze/.venv con s3fs (sin aws-cli en el host).
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
DEST="${1:-backups/$(date +%Y%m%d-%H%M%S)}"

mkdir -p "$DEST/bucket"

echo "==> [1/4] dump del catálogo $CATALOG_DB -> $DEST/catalog.dump"
docker compose exec -T postgres-catalog \
  pg_dump -U "$CATALOG_USER" -d "$CATALOG_DB" -Fc \
  > "$DEST/catalog.dump"
sha256sum "$DEST/catalog.dump" | awk '{print $1}' > "$DEST/catalog.dump.sha256"

echo "==> [2/4] espejo del bucket s3://$LAKEHOUSE_BUCKET/ -> $DEST/bucket/"
"$BRONZE_PYTHON" scripts/s3_sync.py download \
  "s3://$LAKEHOUSE_BUCKET/" "$DEST/bucket/"

echo "==> [3/4] conteos del lago -> $DEST/counts.json"
"$BRONZE_PYTHON" scripts/lake_counts.py > "$DEST/counts.json"

echo "==> [4/4] manifiesto"
DUCKDB_VER="$("$BRONZE_PYTHON" -c "import duckdb; print(duckdb.__version__)" 2>/dev/null || echo "?")"
PG_IMAGE="$(docker inspect kavos_lake-postgres-catalog --format '{{.Config.Image}}' 2>/dev/null || echo "?")"
GARAGE_IMAGE="$(docker inspect kavos_lake-garage --format '{{.Config.Image}}' 2>/dev/null || echo "?")"
cat > "$DEST/manifest.json" <<EOF
{
  "timestamp_utc": "$(date -u +%FT%TZ)",
  "catalog_db": "$CATALOG_DB",
  "bucket": "s3://$LAKEHOUSE_BUCKET/",
  "catalog_dump_bytes": $(stat -c%s "$DEST/catalog.dump"),
  "bucket_files": $(find "$DEST/bucket" -type f | wc -l),
  "bucket_bytes": $(du -sb "$DEST/bucket" | cut -f1),
  "duckdb_py": "$DUCKDB_VER",
  "postgres_image": "$PG_IMAGE",
  "garage_image": "$GARAGE_IMAGE"
}
EOF

cat "$DEST/manifest.json"
echo "backup OK en $DEST"
