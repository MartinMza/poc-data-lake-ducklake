#!/usr/bin/env bash
# Restore idempotente de un .bak en el servicio mssql (OpenSpec mssql-bak-source, tarea 3.1).
#
#   1. verifica que el .bak sea visible dentro del contenedor
#   2. chequeo de versión (RESTORE HEADERONLY: si el .bak es de un SQL Server
#      mayor que la imagen, aborta con mensaje explícito)
#   3. lee los nombres lógicos (RESTORE FILELISTONLY) y genera el
#      RESTORE DATABASE ... WITH MOVE dinámicamente (data/log a los paths
#      del contenedor, renombrados con el nombre de la DB destino)
#   4. restaura con REPLACE (re-corridas convergen al mismo estado)
#   5. verifica post-restore: DB ONLINE + lista tablas con conteo de filas
#
# Uso: ./scripts/restore_mssql.sh <bak> [db_destino]
#   <bak>       nombre del archivo en ./mssql-backup/ o path (se usa su basename)
#   [db_destino] nombre de la DB a crear/sobrescribir; default: basename
#               del .bak sanitizado (ej. test-source.bak -> test_source).
#               Para el .bak REAL del operador, pasar el nombre explícito.
#
# Requiere: servicio mssql levantado y MSSQL_SA_PASSWORD en .env.
# Nota permisos: el motor escribe como usuario mssql del contenedor; un .bak
#   generado ahí queda con owner uid 10001. Para dejarlo legible desde el host:
#   docker compose exec mssql chmod 644 /var/opt/mssql/backup/<file>.bak
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

SA_PW="${MSSQL_SA_PASSWORD:-}"
[[ -n "$SA_PW" ]] || { echo "error: MSSQL_SA_PASSWORD vacía (definila en .env)" >&2; exit 1; }

BAK_ARG="${1:?uso: $0 <bak> [db_destino]}"
BAK_BASE="$(basename "$BAK_ARG")"
HOST_BAK="mssql-backup/$BAK_BASE"
[[ -f "$HOST_BAK" ]] || { echo "error: no existe $HOST_BAK (colocá el .bak en ./mssql-backup/)" >&2; exit 1; }
CONT_BAK="/var/opt/mssql/backup/$BAK_BASE"

TARGET_RAW="${2:-${BAK_BASE%.bak}}"
TARGET="$(printf '%s' "$TARGET_RAW" | tr -c 'A-Za-z0-9_' '_')"
[[ -n "$TARGET" ]] || { echo "error: nombre de DB destino vacío" >&2; exit 1; }
# bracket-quote para T-SQL (TARGET ya viene sanitizado a [A-Za-z0-9_], sin ])
TARGET_Q="$TARGET"
# N-literal para comparaciones por nombre (escapa ')
TARGET_LIT="${TARGET//\'/\'\'}"

COMPOSE=(docker compose)
SQLCMD=("/opt/mssql-tools18/bin/sqlcmd" -S localhost -U sa -P "$SA_PW" -C -b -l 30)
# </dev/null: compose exec reenvía stdin por defecto y vaciaría el stdin de
# los loops (herestring) que llaman a q(); se desacopla siempre.
q() { "${COMPOSE[@]}" exec -T mssql "${SQLCMD[@]}" "$@" < /dev/null; }

echo "==> [1/5] visibilidad del .bak dentro del contenedor"
"${COMPOSE[@]}" exec -T mssql ls -la "$CONT_BAK"

echo "==> [2/5] chequeo de versión (HEADERONLY)"
SERVER_MAJOR="$(q -h -1 -W -Q "SET NOCOUNT ON; SELECT SERVERPROPERTY('ProductMajorVersion');" | tr -d '[:space:]')"
HEADER="$(q -h -1 -W -s "," -Q "SET NOCOUNT ON; RESTORE HEADERONLY FROM DISK = N'$CONT_BAK';" | grep -E '^[A-Za-z0-9_]' | head -1 || true)"
# Campos CSV: 10=DatabaseName, 26=SoftwareVersionMajor (ver comentario: si el
# formato cambia, el parseo falla en validación numérica y se aborta).
BAK_DB="$(echo "$HEADER" | awk -F',' '{print $10}' | xargs || true)"
BAK_MAJOR="$(echo "$HEADER" | awk -F',' '{print $26}' | tr -cd '0-9' || true)"
echo "    servidor=SQL major $SERVER_MAJOR | backup db='$BAK_DB' major=${BAK_MAJOR:-?}"
if [[ "$BAK_MAJOR" =~ ^[0-9]+$ && "$SERVER_MAJOR" =~ ^[0-9]+$ ]]; then
  if (( BAK_MAJOR > SERVER_MAJOR )); then
    echo "error: el .bak fue generado por SQL Server major $BAK_MAJOR, mayor que el de la imagen (major $SERVER_MAJOR)." >&2
    echo "Acción: subir la imagen de mssql a una >= versión origen (ver design.md decisión 1)." >&2
    exit 1
  fi
else
  echo "aviso: no se pudo parsear la versión del backup; se intenta el restore y se valida el error" >&2
fi

echo "==> [3/5] nombres lógicos (FILELISTONLY) + MOVE dinámico"
DATA_DIR="$(q -h -1 -W -Q "SET NOCOUNT ON; SELECT SERVERPROPERTY('InstanceDefaultDataPath');" | tr -d '[:space:]')"
LOG_DIR="$(q -h -1 -W -Q "SET NOCOUNT ON; SELECT SERVERPROPERTY('InstanceDefaultLogPath');" | tr -d '[:space:]')"
echo "    data dir: $DATA_DIR | log dir: $LOG_DIR"
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
      dest="$DATA_DIR${TARGET}_$(printf '%s' "$logical" | tr -c 'A-Za-z0-9_-' '_').$ext"
      ;;
    L)
      dest="$LOG_DIR${TARGET}_$(printf '%s' "$logical" | tr -c 'A-Za-z0-9_-' '_').ldf"
      ;;
    *) continue ;;
  esac
  logical_q="${logical//\'/\'\'}"
  MOVES+=("MOVE N'$logical_q' TO N'$dest'")
  echo "    MOVE: [$logical] ($ftype) -> $dest"
done < <(q -h -1 -W -s "," -Q "SET NOCOUNT ON; RESTORE FILELISTONLY FROM DISK = N'$CONT_BAK';" | grep -E ',(D|L),')
[[ ${#MOVES[@]} -gt 0 ]] || { echo "error: FILELISTONLY no devolvió archivos D/L" >&2; exit 1; }
MOVE_CLAUSE="$(IFS=,; echo "${MOVES[*]}")"

echo "==> [4/5] restore de '$BAK_BASE' a DB [$TARGET] (idempotente: REPLACE)"
RESTORE_SQL="IF DB_ID(N'$TARGET_LIT') IS NOT NULL ALTER DATABASE [$TARGET_Q] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
RESTORE DATABASE [$TARGET_Q] FROM DISK = N'$CONT_BAK' WITH REPLACE, RECOVERY, $MOVE_CLAUSE;
ALTER DATABASE [$TARGET_Q] SET MULTI_USER;"
if ! OUT="$(q -Q "$RESTORE_SQL" 2>&1)"; then
  echo "$OUT" >&2
  if echo "$OUT" | grep -qiE '3169|3143|3241|was backed up on a server running version|newer version|incompatible'; then
    echo "error: .bak incompatible con la imagen (mismatch de versión). Acción: subir la imagen de mssql." >&2
  fi
  exit 1
fi
echo "    restore OK"

echo "==> [5/5] verificación post-restore"
STATE="$(q -h -1 -W -Q "SET NOCOUNT ON; SELECT state_desc FROM sys.databases WHERE name = N'$TARGET_LIT';" | tr -d '[:space:]')"
echo "    estado DB [$TARGET]: $STATE"
[[ "$STATE" == "ONLINE" ]] || { echo "error: la DB no quedó ONLINE (estado=$STATE)" >&2; exit 1; }
TABLES="$(q -h -1 -W -d "$TARGET" -Q "SET NOCOUNT ON; SELECT SCHEMA_NAME(schema_id) + '.' + name FROM sys.tables ORDER BY 1;")"
[[ -n "${TABLES//[[:space:]]/}" ]] || { echo "error: la DB no tiene tablas de usuario" >&2; exit 1; }
while IFS= read -r t; do
  t="$(echo "$t" | xargs)"
  [[ -n "$t" ]] || continue
  # bracket-quote por parte (esquema.tabla)
  IFS='.' read -r sch tab <<< "$t"
  tq="[${sch//\]/\]\]}].[${tab//\]/\]\]}]"
  n="$(q -h -1 -W -d "$TARGET" -Q "SET NOCOUNT ON; SELECT COUNT(*) FROM $tq;" | tr -d '[:space:]')"
  echo "    $t: $n filas"
done <<< "$TABLES"

echo "restore OK: [$TARGET] ONLINE desde $BAK_BASE (re-ejecutable sin duplicados)"
