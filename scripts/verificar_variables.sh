#!/usr/bin/env bash
# verificar_variables.sh — comprueba que la cadena de variables llegue entera.
#
# La cadena es:  .env  +  variables del entorno (Portainer)  ->  docker compose
#                ->  variables de cada contenedor  ->  conexión al catálogo.
#
# Existe porque un desajuste en cualquier eslabón se ve igual desde afuera:
#   FATAL: password authentication failed for user "ducklake"
# y ese mensaje no dice cuál de los dos lados quedó viejo.
#
# No imprime secretos: muestra largos y si coinciden entre fuentes.
# No modifica nada: sólo lee y prueba una conexión.
#
# Uso: ./scripts/verificar_variables.sh
# Salida: 0 si todo coincide, 1 si hay desajuste.
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

CLAVES=(CATALOG_USER CATALOG_PASSWORD CATALOG_DB)
fallas=0
ok()   { echo "  ✔ $*"; }
mal()  { echo "  ✘ $*"; fallas=$((fallas + 1)); }
info() { echo "  · $*"; }

echo "== 1. Fuentes de variables =="
if [[ -f .env ]]; then
  ok ".env presente ($(wc -l < .env) líneas)"
else
  info "no hay .env: compose usa los defaults del propio archivo"
fi

TENIDAS_EN_ENTORNO=()
for c in "${CLAVES[@]}"; do
  [[ -n "${!c:-}" ]] && TENIDAS_EN_ENTORNO+=("$c")
done
if (( ${#TENIDAS_EN_ENTORNO[@]} )); then
  info "el entorno define: ${TENIDAS_EN_ENTORNO[*]} (en Portainer, esto pisa al .env)"
else
  info "el entorno no define ninguna de las claves del catálogo"
fi

echo "== 2. Lo que resuelve docker compose =="
RESUELTO="$(docker compose config --format json 2>/dev/null)" || { mal "docker compose config falló"; exit 1; }

leer_resuelto() {  # $1=clave ; busca en postgres-catalog o en lake-init
  # El JSON viaja por stdin: interpolarlo dentro de un heredoc lo rompe en
  # cuanto aparece una comilla o un `$` en un valor.
  printf '%s' "$RESUELTO" | python3 -c '
import json, sys
clave = sys.argv[1]
cfg = json.load(sys.stdin)
for svc in ("postgres-catalog", "lake-init"):
    env = cfg.get("services", {}).get(svc, {}).get("environment", {}) or {}
    if clave in env:
        print(env[clave])
        break
' "$1"
}

leer_dotenv() {  # $1=clave
  [[ -f .env ]] || return 0
  python3 - "$1" <<'PY'
import sys, pathlib
clave = sys.argv[1]
for linea in pathlib.Path(".env").read_text(encoding="utf-8").splitlines():
    if linea.startswith(clave + "="):
        v = linea.split("=", 1)[1]
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        print(v)
        break
PY
}

declare -A RESUELTAS
for c in "${CLAVES[@]}"; do
  valor="$(leer_resuelto "$c")"
  RESUELTAS[$c]="$valor"
  origen="default del compose"
  [[ -n "$(leer_dotenv "$c")" ]] && origen=".env"
  [[ -n "${!c:-}" ]] && origen="entorno (gana sobre .env)"
  info "$c = ${#valor} caracteres (origen: $origen)"
done

echo "== 3. Conflictos entre fuentes =="
conflicto=0
for c in "${CLAVES[@]}"; do
  de_dotenv="$(leer_dotenv "$c")"
  del_entorno="${!c:-}"
  if [[ -n "$de_dotenv" && -n "$del_entorno" && "$de_dotenv" != "$del_entorno" ]]; then
    mal "$c difiere entre .env (${#de_dotenv} car.) y el entorno (${#del_entorno} car.): el entorno gana"
    conflicto=1
  fi
done
(( conflicto == 0 )) && ok "no hay conflictos: cada clave viene de una sola fuente"

echo "== 4. Lo que recibieron los contenedores =="
for svc in postgres-catalog lake-init; do
  cid="$(docker compose ps -aq "$svc" 2>/dev/null | head -1)"
  if [[ -z "$cid" ]]; then
    info "$svc: no hay contenedor todavía"
    continue
  fi
  case "$svc" in
    postgres-catalog) var=POSTGRES_PASSWORD ;;
    *)                var=CATALOG_PASSWORD ;;
  esac
  del_contenedor="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null \
    | grep "^${var}=" | cut -d= -f2-)"
  if [[ -z "$del_contenedor" ]]; then
    info "$svc: no expone $var"
  elif [[ "$del_contenedor" == "${RESUELTAS[CATALOG_PASSWORD]}" ]]; then
    ok "$svc: coincide con lo resuelto (${#del_contenedor} car.)"
  else
    mal "$svc: tiene ${#del_contenedor} car. pero compose resuelve ${#RESUELTAS[CATALOG_PASSWORD]}: contenedor viejo, falta recrearlo"
  fi
done

echo "== 5. ¿El catálogo acepta esa password? (prueba real, desde otro contenedor) =="
CID_PG="$(docker compose ps -q postgres-catalog 2>/dev/null | head -1)"
if [[ -z "$CID_PG" ]]; then
  info "postgres-catalog no está corriendo: no se puede probar"
else
  RED="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$CID_PG" 2>/dev/null | awk '{print $1}')"
  USUARIO="${RESUELTAS[CATALOG_USER]:-ducklake}"
  BASE="${RESUELTAS[CATALOG_DB]:-ducklake_catalog}"
  # Se conecta por TCP desde otro contenedor: dentro del propio postgres el
  # socket y el loopback son `trust`, así que probar ahí adentro no prueba nada.
  if docker run --rm --network "$RED" -e PGPASSWORD="${RESUELTAS[CATALOG_PASSWORD]}" \
       postgres:17.6-bookworm psql -h postgres-catalog -U "$USUARIO" -d postgres -tAc 'select 1' >/dev/null 2>&1; then
    ok "el catálogo acepta la password que resuelve compose"
  else
    mal "el catálogo RECHAZA la password que resuelve compose: el volumen conserva la de su primer arranque"
    info "arreglo sin perder datos: docker compose exec -T postgres-catalog psql -U $USUARIO -d postgres -c \"ALTER USER \\\"$USUARIO\\\" WITH PASSWORD '<la del .env>';\""
    info "o, si es un despliegue sin datos propios: docker compose down -v && docker compose up -d"
  fi
fi

echo
if (( fallas == 0 )); then
  echo "Cadena de variables completa: .env -> compose -> contenedores -> catálogo."
  exit 0
fi
echo "$fallas verificación(es) fallaron."
exit 1
