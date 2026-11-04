#!/usr/bin/env bash
# verify_stack.sh — preflight del scheduler de ingesta antes de confiar en él.
#
# Verifica lo que falló de verdad al menos una vez:
#   1. el compose es válido
#   2. el servicio está corriendo
#   3. el código del contenedor es el del repo  <-- recrear sin rebuild
#   4. las dependencias del extractor están en la imagen
#   5. el estado persistido es legible
#   6. la retención dejó un solo artefacto fechado
#   7. la API REST y el MCP están arriba y sirven el código actual
#      (nadie avisaba cuando estaban caídos: el MCP quedaba inaccesible
#      en /mcp sin que ninguna verificación lo notara)
#
# Uso: ./scripts/verify_stack.sh   (exit 0 = todo bien, 1 = hay fallas)
# No imprime secretos: nunca corre `docker compose config` sin --quiet.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SERVICIO="${VERIFY_SERVICE:-prefect-serve}"
FALLAS=0

ok()  { printf '  \033[92m✔\033[0m %s\n' "$1"; }
mal() { printf '  \033[91m✘\033[0m %s\n' "$1"; FALLAS=$((FALLAS + 1)); }
info(){ printf '  \033[2m· %s\033[0m\n' "$1"; }

echo "Verificación del scheduler de ingesta ($SERVICIO)"
echo

# 1. compose válido (--quiet: no renderiza variables, no filtra credenciales)
if docker compose config --quiet 2>/dev/null; then
  ok "docker-compose.yml válido"
else
  mal "docker-compose.yml inválido (corré 'docker compose config' y mirá el error)"
fi

# 2. servicio corriendo
ESTADO="$(docker compose ps "$SERVICIO" --format '{{.State}}' 2>/dev/null | head -1)"
if [[ "$ESTADO" == "running" ]]; then
  ok "el servicio está corriendo"
else
  mal "el servicio no está corriendo (estado: ${ESTADO:-ausente}); 'docker compose up -d $SERVICIO'"
fi

if [[ "$ESTADO" == "running" ]]; then
  # 3. frescura del código: el bug de recrear sin rebuild
  DESACTUALIZADOS=0
  for archivo in orchestration/flow.py orchestration/serve.py \
                 orchestration/kavos_acquire/pipeline.py \
                 orchestration/kavos_acquire/retention.py; do
    HOST_SHA="$(sha256sum "$archivo" | cut -d' ' -f1)"
    CONT_SHA="$(docker compose exec -T "$SERVICIO" sha256sum "/workspace/$archivo" 2>/dev/null | cut -d' ' -f1)"
    if [[ "$HOST_SHA" != "$CONT_SHA" ]]; then
      mal "código viejo en el contenedor: $archivo"
      DESACTUALIZADOS=$((DESACTUALIZADOS + 1))
    fi
  done
  if (( DESACTUALIZADOS == 0 )); then
    ok "el código del contenedor coincide con el repo"
  else
    info "arreglo: docker compose build $SERVICIO && docker compose up -d --force-recreate $SERVICIO"
  fi

  # 4. dependencias del extractor dentro de la imagen
  if docker compose exec -T "$SERVICIO" /workspace/bronze/.venv/bin/python \
       -c "import pyodbc, arrow_odbc, pyarrow, duckdb" 2>/dev/null; then
    ok "dependencias del extractor presentes (pyodbc, arrow_odbc, pyarrow, duckdb)"
  else
    mal "faltan dependencias del extractor en la imagen; bak-extract va a fallar"
  fi

  # 5. estado persistido legible
  if docker compose exec -T "$SERVICIO" /workspace/orchestration/.venv/bin/python -c "
import json, pathlib, sys
ruta = pathlib.Path('/workspace/state/.acquire_state.json')
if not ruta.is_file():
    print('sin estado todavía (primera corrida pendiente)'); sys.exit(0)
d = json.loads(ruta.read_text())
aplicado = d.get('applied')
print('UID', d.get('last_uid'), '| aplicado:', (aplicado or {}).get('filename', 'ninguno'))
" 2>/dev/null; then
    ok "estado persistido legible"
  else
    mal "estado persistido ilegible: la guarda de antigüedad no puede operar (borrarlo la desactiva)"
  fi
fi

# 6. retención: un solo artefacto fechado (los fixtures no cuentan)
FECHADOS=0
while IFS= read -r archivo; do
  [[ -n "$archivo" ]] || continue
  if [[ "$(basename "$archivo")" =~ ^.+.Kavos_[0-9]{4}_[0-9]{2}_[0-9]{2}_[0-9]{6}_[0-9]{6}\.bak(\.tar\.(gz|bz2|xz))?$ ]]; then
    FECHADOS=$((FECHADOS + 1))
  fi
done < <(find mssql-backup -maxdepth 1 -type f -iname '*.bak*' 2>/dev/null)

case "$FECHADOS" in
  0) info "mssql-backup/ sin backups fechados todavía" ;;
  1) ok  "mssql-backup/ tiene exactamente un artefacto fechado" ;;
  *) mal "mssql-backup/ tiene $FECHADOS artefactos fechados (se esperaba 1); corré scripts/prune_backups.py --apply" ;;
esac

# --- lake-api: REST + MCP ---------------------------------------------------
# El MCP vive en /mcp (Streamable HTTP) del mismo servicio. Estuvo días caído sin que
# nada lo detectara: por eso se verifica acá.
ESTADO_API="$(docker compose ps lake-api --format '{{.State}}' 2>/dev/null | head -1)"
if [[ "$ESTADO_API" != "running" ]]; then
  mal "lake-api no está corriendo: el MCP en /mcp queda inaccesible; 'docker compose up -d lake-api'"
else
  API_DESACTUALIZADOS=0
  for archivo in src/kavos_lake/adapters/inbound/mcp/server.py \
                 src/kavos_lake/adapters/outbound/duckdb_engine.py; do
    HOST_SHA="$(sha256sum "$archivo" | cut -d' ' -f1)"
    CONT_SHA="$(docker compose exec -T lake-api sha256sum "/app/$archivo" 2>/dev/null | cut -d' ' -f1)"
    if [[ "$HOST_SHA" != "$CONT_SHA" ]]; then
      mal "código viejo en lake-api: $archivo"
      API_DESACTUALIZADOS=$((API_DESACTUALIZADOS + 1))
    fi
  done
  if (( API_DESACTUALIZADOS == 0 )); then
    ok "el código de lake-api coincide con el repo"
  else
    info "arreglo: docker compose up -d --build lake-api"
  fi

  CODIGO_SALUD="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://localhost:${API_PORT:-8000}/health" 2>/dev/null)"
  if [[ "$CODIGO_SALUD" == "200" ]]; then
    ok "GET /health responde 200"
  else
    mal "GET /health devolvió ${CODIGO_SALUD:-sin respuesta}; 'docker compose logs lake-api'"
  fi

  # 401 sin credencial es la respuesta correcta: prueba que falla cerrado, sin
  # manejar secretos en este script.
  CODIGO_MCP="$(curl -s -o /dev/null -m 8 -w '%{http_code}' -X POST \
      -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' \
      -d '{}' "http://localhost:${API_PORT:-8000}/mcp" 2>/dev/null)"
  case "$CODIGO_MCP" in
    401) ok "POST /mcp responde 401 sin credencial (fail-closed)" ;;
    000) mal "POST /mcp no responde: el MCP está caído" ;;
    *)   info "POST /mcp devolvió $CODIGO_MCP (se esperaba 401 sin credencial)" ;;
  esac

  # El middleware de auth responde 401 ANTES de rutear, así que un mount roto
  # también daría 401 y el chequeo de arriba pasaría igual. Esto verifica que la
  # ruta exista de verdad en la app.
  RUTAS_MCP="$(docker compose exec -T lake-api python -c \
      "from kavos_lake.adapters.inbound.http.api import app; print(','.join(getattr(r, 'path', '') or '' for r in app.routes))" 2>/dev/null | tr -d '\r')"
  if [[ ",$RUTAS_MCP," == *",/mcp,"* ]]; then
    ok "el MCP está montado en /mcp"
  else
    mal "el MCP no está montado en /mcp (rutas: ${RUTAS_MCP:-no se pudo leer})"
  fi
fi

echo
if (( FALLAS == 0 )); then
  printf '\033[92mTodo en orden.\033[0m\n'
  exit 0
fi
printf '\033[91m%d verificación(es) fallaron.\033[0m\n' "$FALLAS"
exit 1
