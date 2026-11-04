#!/usr/bin/env bash
# container_init.sh — Inicialización automática 1-clic dentro de Docker (Portainer / Compose).
#
# Se ejecuta automáticamente en el servicio `lake-init` al levantar el stack:
#   1. Crea la base del catálogo DuckLake si no existe
#   2. Si hay un .tar o .bak en ./mssql-backup/, lo descomprime y elige el .bak
#   3. Extrae el .bak a Parquet (`bak2parquet --engine url`, restaura temporal
#      en SQL Server y limpia al terminar) y lo carga en Bronze
#      (`bronze/load_parquet.py`, reemplazo total por tabla)
#   3b. Solo si la ingesta tuvo éxito: aplica la retención del proyecto
#      (un solo artefacto fechado, vía kavos_acquire.retention) y registra
#      el backup como aplicado (vía kavos_acquire.pipeline.mark_applied)
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

  echo "    Aplicando retención (un solo artefacto) y marca de aplicado..."
  # Finalización del bootstrap, solo si la ingesta tuvo éxito (si
  # load_parquet.py fallaba, `set -e` ya habría abortado antes de llegar acá).
  # Reusa orchestration/kavos_acquire como el flow: retention deja un solo
  # artefacto fechado (el .bak extraído es derivado y se regenera) y
  # pipeline.mark_applied registra lo ingerido para la guarda de antigüedad.
  # Nunca aborta el bootstrap si esto falla, pero avisa FUERTE por stderr.
  ORCH_PY="/workspace/orchestration/.venv/bin/python"
  if ! ACQUIRE_STATE_PATH="${ACQUIRE_STATE_PATH:-/workspace/orchestration/.acquire_state.json}" \
      ACQUIRE_FINAL_DIR="${ACQUIRE_FINAL_DIR:-/workspace/mssql-backup}" \
      BOOTSTRAP_ARCHIVE="${NEWEST_TAR:-}" \
      BOOTSTRAP_BAK="${SELECTED_BAK:-}" \
      PYTHONPATH="/workspace/orchestration${PYTHONPATH:+:$PYTHONPATH}" \
      "$ORCH_PY" - <<'PYEOF'
import os
import sys
from pathlib import Path
from kavos_acquire.identity import BackupNameError, parse_backup_id
from kavos_acquire.pipeline import mark_applied
from kavos_acquire.retention import (
    DEFAULT_KEEP,
    apply_retention,
    plan_retention,
    remove_extracted_bak,
)

final_dir = Path(os.environ.get("ACQUIRE_FINAL_DIR", "/workspace/mssql-backup"))
# Misma resolución que orchestration/flow.py: os.getenv("ACQUIRE_STATE_PATH",
# ACQUIRE_STATE_DEFAULT).
state_path = os.environ.get(
    "ACQUIRE_STATE_PATH", "/workspace/orchestration/.acquire_state.json")
archive = os.environ.get("BOOTSTRAP_ARCHIVE", "")
extraido = os.environ.get("BOOTSTRAP_BAK", "")

# 1. El .bak extraído es derivado del comprimido: se borra y queda el
#    artefacto de registro. Sin comprimido no se toca nada (sería la única
#    copia); los fixtures no parsean como fechados y nunca se borran.
if archive and extraido:
    if remove_extracted_bak(Path(archive), Path(extraido)):
        print(f"    Bootstrap: borrado .bak derivado {Path(extraido).name}")
# 2. Poda a un solo artefacto fechado (misma función que usa el flow).
plan = plan_retention(final_dir, keep=DEFAULT_KEEP)
for _identidad, ruta in apply_retention(plan):
    print(f"    Bootstrap: podado artefacto anterior {ruta.name}")
print(f"    Bootstrap: retención {plan.describe()}")
# 3. Marca de aplicado sobre el artefacto de registro (el comprimido cuando
#    existe, como hace el flow con el source extraído); sin marca la guarda
#    de antigüedad no tiene referencia y aceptaría cualquier artefacto.
candidato = archive if archive else extraido
try:
    identidad = parse_backup_id(Path(candidato).name)
except BackupNameError as exc:
    print(f"ADVERTENCIA bootstrap: no se pudo registrar el aplicado "
          f"({exc}); la guarda de antigüedad sigue sin referencia",
          file=sys.stderr)
    sys.exit(1)
mark_applied(state_path, identidad)
print(f"    Bootstrap: aplicado {identidad.filename} registrado en {state_path}")
PYEOF
  then
    echo "ADVERTENCIA: falló la finalización del bootstrap (retención o marca de aplicado); revisá los mensajes de arriba: puede haber quedado el .bak derivado en mssql-backup/ o el estado sin aplicado" >&2
  fi
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
