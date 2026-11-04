#!/usr/bin/env bash
# container_init.sh — Inicialización automática 1-clic dentro de Docker (Portainer / Compose).
#
# Se ejecuta automáticamente en el servicio `lake-init` al levantar el stack:
#   1. Crea la base del catálogo DuckLake si no existe
#   2. Si hay un .tar o .bak en ./mssql-backup/, lo descomprime y lo restaura en SQL Server
#   3. Ingesta esas tablas en Bronze (fast path bcp/MERGE; dlt como fallback)
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
  CONT_BAK="/var/opt/mssql/backup/$BAK_BASENAME"
  echo "    Backup detectado: $BAK_BASENAME"

  SQLCMD=(/opt/mssql-tools18/bin/sqlcmd -S "${MSSQL_HOST},${MSSQL_PORT}" -U sa -P "$MSSQL_SA_PASSWORD" -C -b -l 30)
  for _ in $(seq 1 30); do
    if "${SQLCMD[@]}" -Q "SELECT 1" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done

  if [[ -n "${MSSQL_DATABASE:-}" ]]; then
    TARGET_DB="$MSSQL_DATABASE"
  elif [[ "$BAK_BASENAME" == FAF12_Kavos* ]]; then
    TARGET_DB="FAF12_Kavos"
  elif [[ "$BAK_BASENAME" == "test-source.bak" ]]; then
    TARGET_DB="RestoredProof"
  else
    TARGET_DB="$(printf '%s' "${BAK_BASENAME%.bak}" | tr -c 'A-Za-z0-9_' '_')"
  fi

  DB_ONLINE="$("${SQLCMD[@]}" -h -1 -W -Q "SET NOCOUNT ON; SELECT state_desc FROM sys.databases WHERE name = N'$TARGET_DB';" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ "$DB_ONLINE" != "ONLINE" ]]; then
    echo "    Restaurando $BAK_BASENAME en SQL Server [$TARGET_DB]..."
    DATA_DIR="$("${SQLCMD[@]}" -h -1 -W -Q "SET NOCOUNT ON; SELECT SERVERPROPERTY('InstanceDefaultDataPath');" | tr -d '[:space:]')"
    LOG_DIR="$("${SQLCMD[@]}" -h -1 -W -Q "SET NOCOUNT ON; SELECT SERVERPROPERTY('InstanceDefaultLogPath');" | tr -d '[:space:]')"
    MOVES=()
    DATA_N=0
    while IFS=',' read -r logical _physical ftype _rest; do
      logical="$(echo "$logical" | xargs)"
      ftype="$(echo "$ftype" | xargs)"
      [[ -n "$logical" ]] || continue
      case "$ftype" in
        D)
          DATA_N=$((DATA_N + 1))
          ext="ndf"; [[ $DATA_N -eq 1 ]] && ext="mdf"
          dest="${DATA_DIR}${TARGET_DB}_$(printf '%s' "$logical" | tr -c 'A-Za-z0-9_-' '_').$ext"
          ;;
        L)
          dest="${LOG_DIR}${TARGET_DB}_$(printf '%s' "$logical" | tr -c 'A-Za-z0-9_-' '_').ldf"
          ;;
        *) continue ;;
      esac
      MOVES+=("MOVE N'${logical//\'/\'\'}' TO N'$dest'")
    done < <("${SQLCMD[@]}" -h -1 -W -s "," -Q "SET NOCOUNT ON; RESTORE FILELISTONLY FROM DISK = N'$CONT_BAK';")
    MOVE_CLAUSE="$(IFS=', '; echo "${MOVES[*]}")"
    "${SQLCMD[@]}" -Q "IF DB_ID(N'$TARGET_DB') IS NOT NULL ALTER DATABASE [$TARGET_DB] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; RESTORE DATABASE [$TARGET_DB] FROM DISK = N'$CONT_BAK' WITH $MOVE_CLAUSE, REPLACE, RECOVERY; ALTER DATABASE [$TARGET_DB] SET MULTI_USER;"
  else
    echo "    Base [$TARGET_DB] ya está ONLINE en SQL Server."
  fi

  export MSSQL_DATABASE="$TARGET_DB"
  if [[ -z "${MSSQL_TABLES:-}" && "$TARGET_DB" == "FAF12_Kavos" ]]; then
    export MSSQL_TABLES="BSMoneda:MonedaID:merge,BSProducto:ProductoID:merge,BSOrganizacion:OrganizacionID:merge,BSTransaccion:TransaccionID:merge,BSOperacion:TransaccionID:merge,DMart_Ventas::replace,DMart_VentasProductoCliente::replace"
    export MSSQL_PIPELINE_NAME="${MSSQL_PIPELINE_NAME:-bronze_mssql_faf12}"
  fi

  echo "==> [3/4] Ingestando datos de MSSQL [$TARGET_DB] hacia DuckLake (Bronze)..."
  /workspace/bronze/.venv/bin/python bronze/fast_mssql.py all
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
