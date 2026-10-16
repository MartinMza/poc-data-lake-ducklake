#!/usr/bin/env bash
# Bootstrap idempotente del lakehouse (OpenSpec poc-data-lake-ducklake, tarea 2.2).
#
# Crea/verifica, sin duplicar en re-ejecuciones:
#   1. base del catálogo DuckLake en postgres-catalog (database PostgreSQL;
#      las tablas de metadata DuckLake se crean lazy en el primer ATTACH,
#      ver scripts/smoke_ducklake.py)
#   2. bucket en Garage (vía auto-provisión --default-bucket + create si falta)
#   3. credenciales S3 con RWO sobre el bucket (re-allow idempotente)
#
# Las credenciales S3 son las auto-provisionadas por Garage desde
# GARAGE_DEFAULT_* (fijas en docker-compose.yml, solo-PoC-local), por lo que
# no hay secreto que recuperar: re-ejecutar nunca rota ni duplica nada.
#
# Uso: ./scripts/bootstrap.sh [--skip-prefect-check]
set -euo pipefail

SKIP_PREFECT_CHECK=false
if [[ "${1:-}" == "--skip-prefect-check" ]]; then SKIP_PREFECT_CHECK=true; fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# .env opcional; defaults idénticos a los de docker-compose.yml.
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

CATALOG_DB="${CATALOG_DB:-ducklake_catalog}"
CATALOG_USER="${CATALOG_USER:-ducklake}"
CATALOG_PORT="${CATALOG_PORT:-5433}"
LAKEHOUSE_BUCKET="${LAKEHOUSE_BUCKET:-lakehouse}"
GARAGE_S3_KEY_ID="${GARAGE_S3_KEY_ID:-GK0123456789abcdef0123456789abcdef}"
PREFECT_PORT="${PREFECT_PORT:-4200}"

COMPOSE=(docker compose)
if ! "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -q .; then
  echo "error: el stack no está levantado; primero 'docker compose up -d'" >&2
  exit 1
fi

echo "==> [1/4] esperando postgres-catalog ..."
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T postgres-catalog \
      pg_isready -U "$CATALOG_USER" -d "$CATALOG_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
"${COMPOSE[@]}" exec -T postgres-catalog \
  pg_isready -U "$CATALOG_USER" -d "$CATALOG_DB"

echo "==> [2/4] base del catálogo '$CATALOG_DB' ..."
DB_EXISTS="$("${COMPOSE[@]}" exec -T postgres-catalog psql -U "$CATALOG_USER" \
  -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$CATALOG_DB'")"
if [[ "$DB_EXISTS" != "1" ]]; then
  echo "    no existe: creando database $CATALOG_DB"
  "${COMPOSE[@]}" exec -T postgres-catalog psql -U "$CATALOG_USER" \
    -d postgres -c "CREATE DATABASE \"$CATALOG_DB\""
else
  echo "    ya existe (idempotente, sin cambios)"
fi
"${COMPOSE[@]}" exec -T postgres-catalog psql -U "$CATALOG_USER" \
  -d "$CATALOG_DB" -c "SELECT 1 AS catalog_reachable" >/dev/null
echo "    catálogo acepta conexiones"

echo "==> [3/4] esperando garage ..."
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T garage /garage status >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
"${COMPOSE[@]}" exec -T garage /garage status | grep -E "HEALTHY|ID" || true

echo "==> [4/4] bucket '$LAKEHOUSE_BUCKET' + credenciales S3 ..."
if "${COMPOSE[@]}" exec -T garage /garage bucket info "$LAKEHOUSE_BUCKET" \
    >/dev/null 2>&1; then
  echo "    bucket ya existe (idempotente, sin cambios)"
else
  echo "    no existe: creando bucket $LAKEHOUSE_BUCKET"
  "${COMPOSE[@]}" exec -T garage /garage bucket create "$LAKEHOUSE_BUCKET"
fi
# Re-allow es idempotente: garantiza RWO de la key sobre el bucket.
"${COMPOSE[@]}" exec -T garage /garage bucket allow \
  --read --write --owner "$LAKEHOUSE_BUCKET" --key "$GARAGE_S3_KEY_ID" \
  | grep -E "Permissions|RWO|$GARAGE_S3_KEY_ID" || true

if [[ "$SKIP_PREFECT_CHECK" == "false" ]]; then
  echo "==> [extra] prefect-server (informativo, no bloquea) ..."
  if curl -sf -o /dev/null --max-time 5 \
      "http://localhost:${PREFECT_PORT}/api/health"; then
    echo "    prefect-server healthy en :${PREFECT_PORT}"
  else
    echo "    aviso: prefect-server aún no responde en :${PREFECT_PORT}" >&2
  fi
fi

cat <<EOF
==> bootstrap OK (re-ejecutable sin duplicados)
    catalog : postgres://$CATALOG_USER@localhost:$CATALOG_PORT/$CATALOG_DB
    bucket  : s3://$LAKEHOUSE_BUCKET/ (endpoint http://localhost:3900)
    s3 key  : $GARAGE_S3_KEY_ID (secret en GARAGE_S3_SECRET del entorno/compose)
    siguiente: python3 scripts/smoke_ducklake.py
EOF
