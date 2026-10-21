#!/usr/bin/env bash
# setup.sh — Setup inicial completo e idempotente del Data Lake Dulse.
#
# Prepara desde cero (o actualiza sin romper estado existente):
#   1. Archivo .env y carpeta ./mssql-backup/ (donde se dejan los archivos .bak)
#   2. Infraestructura Docker (postgres-catalog, postgres-source, garage, prefect-server, mssql)
#   3. Bootstrap del catálogo DuckLake y bucket Garage S3
#   4. Entornos virtuales Python 3.12 con uv (bronze, dbt, orchestration, api)
#   5. Seed de datos iniciales en postgres-source (si está vacío o con --force-seed)
#   6. Detección automática de .bak o .tar (.tar/.tar.gz/.tgz) en ./mssql-backup/:
#      - Si encuentra un .tar, descomprime el .bak automáticamente en ./mssql-backup/
#      - Restaura el .bak más reciente en SQL Server (si es nuevo o con --force-restore)
#      - Ingiere los datos de MSSQL y Postgres hacia el Data Lake (Bronze -> Silver -> Gold)
#   7. Validación de API REST y Servidor MCP
#
# Uso:
#   ./scripts/setup.sh                        # Setup completo + auto-detección de .bak/.tar en ./mssql-backup/
#   ./scripts/setup.sh --bak /ruta/dump.tar   # Extrae el .tar (o copia el .bak) a ./mssql-backup/, restaura y carga datos
#   ./scripts/setup.sh --force-restore        # Fuerza el restore e ingesta completa del .bak actual
#   ./scripts/setup.sh --scale base           # Usa escala 'base' (10k clientes) para postgres-source
#   ./scripts/setup.sh --start-api            # Al finalizar, deja corriendo la API/MCP en Docker (:8000)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCALE="demo"
FORCE_SEED=false
FORCE_RESTORE=false
START_API=false
CUSTOM_BAK=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scale)
      SCALE="${2:-demo}"
      shift 2
      ;;
    --bak)
      CUSTOM_BAK="${2:?Error: --bak requiere la ruta al archivo .bak}"
      shift 2
      ;;
    --force-seed)
      FORCE_SEED=true
      shift
      ;;
    --force-restore)
      FORCE_RESTORE=true
      shift
      ;;
    --start-api)
      START_API=true
      shift
      ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0
      ;;
    *)
      echo "Opción desconocida: $1 (usa --help para ver opciones)" >&2
      exit 1
      ;;
  esac
done

echo "================================================================="
echo "  Dulse Data Lake — Setup Inicial Completo"
echo "================================================================="

# 0. Verificar herramientas requeridas
for cmd in docker uv; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: '$cmd' no está instalado o no está en el PATH." >&2
    exit 1
  fi
done

# 1. Archivo .env y carpeta ./mssql-backup/
echo ""
echo "==> [1/7] Verificando configuración (.env y carpeta ./mssql-backup/)..."
mkdir -p ./mssql-backup
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "    Creado .env a partir de .env.example"
else
  echo "    .env ya existe (se conserva)"
fi

EXTRACTED_BAK=""

extract_tar_to_backup() {
  local tar_file="$1"
  local member=""
  if tar -tf "$tar_file" >/dev/null 2>&1; then
    member="$(tar -tf "$tar_file" | grep -iE '\.bak$' | head -n1 || true)"
  elif docker compose ps --status running --services 2>/dev/null | grep -q "^mssql$"; then
    local cont_tar="/var/opt/mssql/backup/$(basename "$tar_file")"
    member="$(docker compose exec -T --user root mssql tar -tf "$cont_tar" | grep -iE '\.bak$' | head -n1 || true)"
  fi
  if [[ -z "$member" ]]; then
    echo "Error: El archivo '$tar_file' no contiene ningún archivo .bak." >&2
    exit 1
  fi
  local bak_base
  bak_base="$(basename "$member")"
  local dest_bak="./mssql-backup/$bak_base"

  if [[ "$FORCE_RESTORE" == "true" || ! -f "$dest_bak" || "$tar_file" -nt "$dest_bak" ]]; then
    echo "    Descomprimiendo '$tar_file' ($member) -> $dest_bak ..."
    if ! tar -xOf "$tar_file" "$member" > "$dest_bak" 2>/dev/null; then
      local cont_tar="/var/opt/mssql/backup/$(basename "$tar_file")"
      docker compose exec -T --user root mssql tar -xOf "$cont_tar" "$member" > "$dest_bak"
    fi
    chmod 644 "$dest_bak"
  else
    echo "    '$dest_bak' ya fue descomprimido previamente desde '$tar_file'."
  fi
  EXTRACTED_BAK="$dest_bak"
}

if [[ -n "$CUSTOM_BAK" ]]; then
  if [[ ! -f "$CUSTOM_BAK" ]]; then
    echo "Error: No existe el archivo especificado en --bak: $CUSTOM_BAK" >&2
    exit 1
  fi
  case "${CUSTOM_BAK,,}" in
    *.tar|*.tar.gz|*.tgz|*.tar.bz2|*.tar.xz|*.tar.zst)
      extract_tar_to_backup "$CUSTOM_BAK"
      ;;
    *)
      DEST_BAK="./mssql-backup/$(basename "$CUSTOM_BAK")"
      if [[ ! -e "$DEST_BAK" ]] || [[ "$(realpath "$CUSTOM_BAK")" != "$(realpath "$DEST_BAK")" ]]; then
        echo "    Copiando $CUSTOM_BAK -> $DEST_BAK ..."
        cp -f "$CUSTOM_BAK" "$DEST_BAK"
        chmod 644 "$DEST_BAK"
      fi
      EXTRACTED_BAK="$DEST_BAK"
      ;;
  esac
fi

# Cargar variables de .env
set -a
# shellcheck disable=SC1091
source .env
set +a

SOURCE_HOST="${SOURCE_HOST:-localhost}"
SOURCE_USER="${SOURCE_USER:-source_user}"
SOURCE_PASSWORD="${SOURCE_PASSWORD:-source_password}"
SOURCE_DB="${SOURCE_DB:-source_db}"
SOURCE_PORT="${SOURCE_PORT:-5434}"
API_HOST="${API_HOST:-localhost}"
API_PORT="${API_PORT:-8000}"

# 2. Infraestructura Docker
echo ""
echo "==> [2/7] Levantando contenedores base con Docker Compose..."
SERVICES=(postgres-catalog postgres-source garage prefect-server)
if [[ -n "${MSSQL_SA_PASSWORD:-}" ]]; then
  SERVICES+=(mssql)
fi
docker compose up -d "${SERVICES[@]}"

echo "    Esperando a que postgres-source esté listo..."
for _ in $(seq 1 30); do
  if docker compose exec -T postgres-source pg_isready -U "$SOURCE_USER" -d "$SOURCE_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# 3. Bootstrap del Lakehouse (Catálogo + S3 + Prefect check)
echo ""
echo "==> [3/7] Ejecutando bootstrap del Lakehouse (Catálogo + Garage S3)..."
./scripts/bootstrap.sh

# 4. Entornos virtuales Python 3.12 con uv
echo ""
echo "==> [4/7] Configurando entornos virtuales Python 3.12 (uv)..."
for mod in bronze dbt orchestration api; do
  echo "    -> Módulo: $mod"
  if [[ ! -f "$mod/.venv/bin/python" ]]; then
    uv venv "$mod/.venv" --python 3.12
  fi
  uv pip install -p "$mod/.venv/bin/python" -r "$mod/requirements.txt" --quiet
done
echo "    Entornos virtuales listos (bronze, dbt, orchestration, api)."

# 5. Seed de datos en postgres-source
echo ""
echo "==> [5/7] Verificando datos en postgres-source..."
HAS_CUSTOMERS="$(docker compose exec -T postgres-source psql -U "$SOURCE_USER" -d "$SOURCE_DB" -tAc \
  "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name='customers'" 2>/dev/null || echo "0")"

ROW_COUNT="0"
if [[ "$HAS_CUSTOMERS" == "1" ]]; then
  ROW_COUNT="$(docker compose exec -T postgres-source psql -U "$SOURCE_USER" -d "$SOURCE_DB" -tAc \
    "SELECT COUNT(*) FROM customers" 2>/dev/null || echo "0")"
fi

if [[ "$FORCE_SEED" == "true" || "$ROW_COUNT" == "0" ]]; then
  echo "    Cargando datos sintéticos en postgres-source (escala: $SCALE)..."
  bronze/.venv/bin/python source/generate.py --mode seed --scale "$SCALE" --out postgres
else
  echo "    postgres-source ya cuenta con datos ($ROW_COUNT clientes). Usa --force-seed para regenerar."
fi

# 6. Pipeline Medallón: Detección de .bak en ./mssql-backup/ + Ingesta + dbt + Export
echo ""
echo "==> [6/7] Ejecutando pipeline de datos (Postgres + MSSQL .bak -> DuckLake)..."
echo "    -> Ingesta Bronze (Postgres -> DuckLake)..."
bronze/.venv/bin/python bronze/pipeline.py

# Si dejaron algún .tar (.tar/.tar.gz/.tgz) dentro de ./mssql-backup/, descomprimirlo primero
if [[ -z "$EXTRACTED_BAK" ]]; then
  NEWEST_TAR="$(find ./mssql-backup -maxdepth 1 \( -iname '*.tar' -o -iname '*.tar.gz' -o -iname '*.tgz' -o -iname '*.tar.bz2' -o -iname '*.tar.xz' \) -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2- || true)"
  if [[ -n "$NEWEST_TAR" ]]; then
    echo "    -> Detectado archivo comprimido en ./mssql-backup/: $(basename "$NEWEST_TAR")"
    extract_tar_to_backup "$NEWEST_TAR"
  fi
fi

# Buscar el .bak más reciente en ./mssql-backup/ (priorizando el extraído o backups reales sobre test-source.bak)
SELECTED_BAK=""
if [[ -n "$EXTRACTED_BAK" ]]; then
  SELECTED_BAK="$EXTRACTED_BAK"
else
  SELECTED_BAK="$(find ./mssql-backup -maxdepth 1 -name '*.bak' ! -name 'test-source.bak' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2- || true)"
  if [[ -z "$SELECTED_BAK" ]]; then
    SELECTED_BAK="$(find ./mssql-backup -maxdepth 1 -name '*.bak' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n1 | cut -d' ' -f2- || true)"
  fi
fi

if [[ -n "$SELECTED_BAK" ]] && docker compose ps --status running --services | grep -q "^mssql$"; then
  BAK_BASENAME="$(basename "$SELECTED_BAK")"
  echo "    -> Backup MSSQL detectado en ./mssql-backup/: $BAK_BASENAME"

  # Asegurar que el contenedor mssql responda antes del restore
  echo "    -> Esperando a que SQL Server esté saludable..."
  for _ in $(seq 1 30); do
    if docker compose exec -T mssql /opt/mssql-tools18/bin/sqlcmd \
         -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -Q "SELECT 1" -C -l 5 >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  # Determinar nombre de base de datos destino
  if [[ -n "${MSSQL_DATABASE:-}" ]]; then
    TARGET_DB="$MSSQL_DATABASE"
  elif [[ "$BAK_BASENAME" == FAF12_Kavos* ]]; then
    TARGET_DB="FAF12_Kavos"
  elif [[ "$BAK_BASENAME" == "test-source.bak" ]]; then
    TARGET_DB="RestoredProof"
  else
    TARGET_DB="$(printf '%s' "${BAK_BASENAME%.bak}" | tr -c 'A-Za-z0-9_' '_')"
  fi

  # Verificar si esta DB ya existe y está ONLINE en SQL Server (para no demorar si ya fue restaurada)
  DB_ONLINE="$(docker compose exec -T mssql /opt/mssql-tools18/bin/sqlcmd \
    -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C -h -1 -W \
    -Q "SET NOCOUNT ON; SELECT state_desc FROM sys.databases WHERE name = N'$TARGET_DB';" 2>/dev/null | tr -d '[:space:]' || true)"

  if [[ "$FORCE_RESTORE" == "true" || "$DB_ONLINE" != "ONLINE" ]]; then
    echo "    -> Restaurando $BAK_BASENAME en base [$TARGET_DB]..."
    ./scripts/restore_mssql.sh "$BAK_BASENAME" "$TARGET_DB"
  else
    echo "    -> Base [$TARGET_DB] ya está restaurada y ONLINE en SQL Server (usa --force-restore para re-restaurar)."
  fi

  # Configurar tablas por defecto según la base si MSSQL_TABLES no fue definida
  export MSSQL_DATABASE="$TARGET_DB"
  if [[ -z "${MSSQL_TABLES:-}" && "$TARGET_DB" == "FAF12_Kavos" ]]; then
    export MSSQL_TABLES="BSMoneda:MonedaID:merge,BSProducto:ProductoID:merge,BSOrganizacion:OrganizacionID:merge,BSTransaccion:TransaccionID:merge,BSOperacion:TransaccionID:merge,DMart_Ventas::replace,DMart_VentasProductoCliente::replace"
    export MSSQL_PIPELINE_NAME="${MSSQL_PIPELINE_NAME:-bronze_mssql_faf12}"
  fi

  echo "    -> Ingestando datos desde MSSQL [$TARGET_DB] hacia DuckLake (esquema bronze)..."
  if [[ -x "$HOME/.local/mssql-odbc/bin/bcp" ]] || command -v bcp >/dev/null 2>&1; then
    PATH="$HOME/.local/mssql-odbc/bin:$PATH" bronze/.venv/bin/python bronze/fast_mssql.py all
  else
    bronze/.venv/bin/python bronze/pipeline_mssql.py
  fi
else
  echo "    -> Sin archivos .bak en ./mssql-backup/ (coloca un .bak allí para restaurar e ingerir MSSQL)."
fi

echo "    -> Transformaciones dbt (Silver + Gold)..."
dbt/.venv/bin/dbt run --project-dir dbt --profiles-dir dbt
dbt/.venv/bin/dbt test --project-dir dbt --profiles-dir dbt

echo "    -> Exportando capa Gold a gold_serving (Postgres)..."
bronze/.venv/bin/python scripts/export_gold.py

# 7. Validación de API REST y Servidor MCP
echo ""
echo "==> [7/7] Validando API REST (FastAPI) y Servidor MCP (FastMCP)..."
api/.venv/bin/pytest -q api/test_api_mcp.py

if [[ "$START_API" == "true" ]]; then
  echo ""
  echo "==> Levantando contenedor lake-api en Docker..."
  ./scripts/run_api.sh --docker
fi

echo ""
echo "================================================================="
echo "  Estado Actual del Data Lake (Conteo de Filas)"
echo "================================================================="
bronze/.venv/bin/python scripts/lake_counts.py

echo ""
echo "================================================================="
echo "  Setup Inicial Completado con Éxito"
echo "================================================================="
echo "  Carpeta de backups MSSQL:  ./mssql-backup/"
echo "  Para iniciar la API/MCP:   ./scripts/run_api.sh"
echo "  Swagger UI (API REST):     http://${API_HOST}:${API_PORT}/docs"
echo "  MCP Server (SSE):          http://${API_HOST}:${API_PORT}/mcp/sse"
echo "================================================================="
