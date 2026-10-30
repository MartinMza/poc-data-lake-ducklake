#!/usr/bin/env bash
# Migra la base de credenciales del rename Dulse -> KavosLake.
#
# Qué hace: renombra la base `dulse_auth` a `kavos_lake_auth` (y la de tests).
# No copia ni borra datos: `ALTER DATABASE ... RENAME TO` conserva usuarios,
# API keys, sesiones y tokens tal como están.
#
# Es idempotente y **dry-run por defecto**: sin `--apply` sólo reporta qué haría.
#
# Uso:
#   ./scripts/migrate_rename_kavoslake.sh            # muestra el plan
#   ./scripts/migrate_rename_kavoslake.sh --apply    # ejecuta
#
# Después de migrar hay que apuntar el entorno a la base nueva:
#   .env:  AUTH_DB_NAME=kavos_lake_auth
#   (si AUTH_DB_NAME no está definido, el default ya es `kavos_lake_auth`)
#
# Notas de impacto (esperadas, no errores):
#   - las sesiones del panel se invalidan (cambia el nombre de la cookie);
#   - los JWT emitidos dejan de validar (cambia el issuer);
#   - las API keys viejas siguen funcionando: se buscan por hash, no por prefijo.

set -euo pipefail

OLD_DB="dulse_auth"
NEW_DB="kavos_lake_auth"
OLD_TEST_DB="dulse_auth_test"
NEW_TEST_DB="kavos_lake_auth_test"

APPLY="false"
if [[ "${1:-}" == "--apply" ]]; then
  APPLY="true"
elif [[ "${1:-}" != "" && "${1:-}" != "--dry-run" ]]; then
  echo "Uso: $0 [--apply]" >&2
  exit 2
fi

psql_admin() {
  docker compose exec -T postgres-catalog psql -U ducklake -d postgres -tAc "$1"
}

if ! docker compose ps --services --status running 2>/dev/null | grep -qx "postgres-catalog"; then
  echo "ERROR: el contenedor postgres-catalog no está corriendo." >&2
  echo "       Levantalo con: docker compose up -d postgres-catalog" >&2
  exit 1
fi

exists() {
  [[ "$(psql_admin "SELECT 1 FROM pg_database WHERE datname = '$1'")" == "1" ]]
}

rename_db() {
  local old="$1" new="$2"
  if ! exists "$old"; then
    echo "  - $old: no existe, nada que hacer"
    return 0
  fi
  if exists "$new"; then
    echo "  - $old: ya existe $new; se deja $old intacta (revisar a mano)"
    return 0
  fi
  if [[ "$APPLY" == "true" ]]; then
    psql_admin "ALTER DATABASE \"$old\" RENAME TO \"$new\"" >/dev/null
    echo "  - $old -> $new: RENOMBRADA"
  else
    echo "  - $old -> $new: se renombraría (dry-run)"
  fi
}

echo "==> Migración de nombres: Dulse -> KavosLake"
echo "    modo: $([[ "$APPLY" == "true" ]] && echo APPLY || echo 'dry-run (usá --apply para ejecutar)')"
rename_db "$OLD_DB" "$NEW_DB"
rename_db "$OLD_TEST_DB" "$NEW_TEST_DB"

echo ""
if [[ "$APPLY" != "true" ]]; then
  echo "Dry-run: no se modificó nada. Revisá el plan y volvé a correrlo con --apply."
  exit 0
fi

if psql_admin "SELECT 1 FROM pg_database WHERE datname = '$NEW_DB'" | grep -q 1; then
  echo "Verificación: $NEW_DB existe."
  echo ""
  echo "Pasos manuales que faltan:"
  echo "  1. .env: AUTH_DB_NAME=kavos_lake_auth"
  echo "  2. reiniciar la API para que tome el issuer y la cookie nuevos"
  echo "  3. las sesiones del panel y los JWT viejos dejan de servir: re-logueate"
fi
