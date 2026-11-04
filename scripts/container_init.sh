#!/usr/bin/env bash
# container_init.sh — Inicialización automática 1-clic dentro de Docker (Portainer / Compose).
#
# Se ejecuta automáticamente en el servicio `lake-init` al levantar el stack:
#   1. Crea la base del catálogo DuckLake si no existe
#   2. Si hay un .tar o .bak en ./mssql-backup/, lo descomprime y elige el .bak
#   3. Extrae el .bak a Parquet (`bak2parquet --engine url`, restaura temporal
#      en SQL Server y limpia al terminar) y lo carga en Bronze
#      (`bronze/load_parquet.py`, reemplazo total por tabla)
#   4. Muestra el conteo final del lago en los logs de Portainer
# La única fuente de datos es SQL Server: no hay seed sintético ni capas dbt.

set -euo pipefail

cd /workspace

CATALOG_HOST="${CATALOG_HOST:-postgres-catalog}"
CATALOG_PORT="${CATALOG_PORT:-5432}"
CATALOG_DB="${CATALOG_DB:-ducklake_catalog}"
CATALOG_USER="${CATALOG_USER:-ducklake}"
CATALOG_PASSWORD="${CATALOG_PASSWORD:-ducklake}"

export GARAGE_SCHEME="${GARAGE_SCHEME:-http}"
export GARAGE_HOST="${GARAGE_HOST:-garage}"
export GARAGE_S3_PORT="${GARAGE_S3_PORT:-3900}"

MSSQL_HOST="${MSSQL_HOST:-mssql}"
MSSQL_PORT="${MSSQL_PORT:-1433}"
MSSQL_USER="${MSSQL_USER:-sa}"
MSSQL_SA_PASSWORD="${MSSQL_SA_PASSWORD:-Str0ng!PoC-Local01}"

echo "================================================================="
echo "  KavosLake Data Platform — Auto-Init (Portainer / Docker Compose)"
echo "================================================================="

echo "==> [1/4] Verificando base de catálogo '$CATALOG_DB' en $CATALOG_HOST..."
export PGPASSWORD="$CATALOG_PASSWORD"
for _ in $(seq 1 30); do
  if pg_isready -h "$CATALOG_HOST" -p "$CATALOG_PORT" -U "$CATALOG_USER" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

DB_EXISTS="$(psql -h "$CATALOG_HOST" -p "$CATALOG_PORT" -U "$CATALOG_USER" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='$CATALOG_DB'")"
if [[ "$DB_EXISTS" != "1" ]]; then
  echo "    Creando base de catálogo $CATALOG_DB..."
  psql -h "$CATALOG_HOST" -p "$CATALOG_PORT" -U "$CATALOG_USER" -d postgres -c "CREATE DATABASE \"$CATALOG_DB\""
fi

echo "==> [2/4] Verificando backups de SQL Server (.tar / .bak) en ./mssql-backup/..."
mkdir -p /workspace/mssql-backup

# Si hay un .tar/.tar.gz/.tgz/.tar.bz2/.tar.xz/.tar.zst, extraer el .bak
NEWEST_TAR="$(find /workspace/mssql-backup -maxdepth 1 \( -iname '*.tar' -o -iname '*.tar.gz' -o -iname '*.tgz' -o -iname '*.tar.bz2' -o -iname '*.tar.xz' -o -iname '*.tar.zst' \) -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2- || true)"
if [[ -n "$NEWEST_TAR" ]]; then
  MEMBER="$(tar -tf "$NEWEST_TAR" | grep -iE '\.bak$' | head -n1 || true)"
  if [[ -n "$MEMBER" ]]; then
    DEST_BAK="/workspace/mssql-backup/$(basename "$MEMBER")"
    if [[ ! -f "$DEST_BAK" || "$NEWEST_TAR" -nt "$DEST_BAK" ]]; then
      echo "    Descomprimiendo $(basename "$NEWEST_TAR") ($MEMBER) -> $(basename "$DEST_BAK")..."
      tar -xOf "$NEWEST_TAR" "$MEMBER" > "$DEST_BAK"
      chmod 644 "$DEST_BAK"
    fi
  fi
fi

SELECTED_BAK="$(find /workspace/mssql-backup -maxdepth 1 -name '*.bak' ! -name 'test-source.bak' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2- || true)"
if [[ -z "$SELECTED_BAK" ]]; then
  SELECTED_BAK="$(find /workspace/mssql-backup -maxdepth 1 -name '*.bak' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2- || true)"
fi

if [[ -n "$SELECTED_BAK" ]]; then
  BAK_BASENAME="$(basename "$SELECTED_BAK")"
  echo "    Backup detectado: $BAK_BASENAME"

  echo "==> [3/4] Extrayendo $BAK_BASENAME a Parquet (restaura temporal y limpia)..."
  export PYTHONPATH="/workspace/bak2parquet/src${PYTHONPATH:+:$PYTHONPATH}"
  BRONZE_PY="/workspace/bronze/.venv/bin/python"
  if ! "$BRONZE_PY" -c "import bak2parquet, arrow_odbc" 2>/dev/null; then
    echo "    Instalando dependencias del extractor en el venv bronze..."
    uv pip install -q -p "$BRONZE_PY" -r /workspace/bak2parquet/requirements.txt
  fi
  PARQUET_OUT="${PARQUET_OUT:-/workspace/parquet-out}"
  mkdir -p "$PARQUET_OUT"
  # El password viaja por env (MSSQL_SA_PASSWORD), nunca en la línea de comandos.
  # --bak-path es la ruta vista por el motor (./mssql-backup está montado en
  # /var/opt/mssql/backup del servicio mssql).
  "$BRONZE_PY" -m bak2parquet --engine url \
    --bak "$SELECTED_BAK" \
    --bak-path "/var/opt/mssql/backup/$BAK_BASENAME" \
    --out "$PARQUET_OUT" \
    --mssql-host "$MSSQL_HOST" --mssql-port "$MSSQL_PORT" --mssql-user "$MSSQL_USER" \
    --batch-size "${BAK2PARQUET_BATCH_SIZE:-50000}" \
    --max-text-size "${BAK2PARQUET_MAX_TEXT_SIZE:-8388608}" \
    --max-binary-size "${BAK2PARQUET_MAX_BINARY_SIZE:-8388608}" \
    --report-json "$PARQUET_OUT/report.json"

  echo "    Cargando Parquet hacia DuckLake (Bronze, reemplazo total por tabla)..."
  "$BRONZE_PY" bronze/load_parquet.py --src "$PARQUET_OUT" \
    --report-json "$PARQUET_OUT/bronze-load-report.json"
else
  echo "    Sin archivos .bak/.tar en ./mssql-backup/ (se omite ingesta MSSQL)."
fi

echo "==> [4/4] Conteo final del Data Lake..."
echo ""
echo "================================================================="
echo "  Conteo Final de Tablas en el Data Lake"
echo "================================================================="
/workspace/bronze/.venv/bin/python scripts/lake_counts.py
echo "================================================================="
echo "  Auto-Init finalizado con éxito. API/MCP lista en puerto ${API_PORT:-8000}."
echo "================================================================="
