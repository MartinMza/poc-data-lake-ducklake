#!/usr/bin/env bash
# run_api.sh — Inicia la API REST (FastAPI) y servidor MCP (FastMCP) de Dulse Lakehouse.
#
# Uso:
#   ./scripts/run_api.sh           # Modo host por defecto con recarga en vivo (puerto 8000)
#   ./scripts/run_api.sh --host    # Modo host explícito (usa api/.venv)
#   ./scripts/run_api.sh --docker  # Modo contenedor Docker (docker compose up -d lake-api)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

API_HOST="${API_HOST:-localhost}"
API_BIND_HOST="${API_BIND_HOST:-0.0.0.0}"
API_PORT="${API_PORT:-8000}"
CATALOG_USER="${CATALOG_USER:-ducklake}"
CATALOG_DB="${CATALOG_DB:-ducklake_catalog}"

# Auth fail-closed: sin API keys ni usuarios JWT el servicio no arranca.
if [[ -z "${AUTH_API_KEYS:-}" && -z "${AUTH_USERS:-}" ]]; then
  echo "ERROR: auth sin configurar. Definí AUTH_API_KEYS y/o AUTH_USERS (+ AUTH_JWT_SECRET) en .env." >&2
  echo "       Generá una API key:  openssl rand -hex 24" >&2
  echo "       Generá un secreto JWT: openssl rand -hex 32" >&2
  echo "       Hasheá un password:  api/.venv/bin/python scripts/hash_password.py" >&2
  exit 1
fi
if [[ -n "${AUTH_USERS:-}" && -z "${AUTH_JWT_SECRET:-}" ]]; then
  echo "ERROR: AUTH_USERS está definido pero falta AUTH_JWT_SECRET (openssl rand -hex 32)." >&2
  exit 1
fi
AUTH_KEY_HINT="${AUTH_API_KEYS:-}${AUTH_API_KEYS:+,}"
AUTH_KEY_HINT="${AUTH_KEY_HINT%%,*}"
if [[ -n "$AUTH_KEY_HINT" ]]; then
  AUTH_HINT="header X-API-Key: ${AUTH_KEY_HINT:0:12}…"
else
  AUTH_HINT="POST /auth/token (usuarios de AUTH_USERS)"
fi

MODE="host"
if [[ "${1:-}" == "--docker" ]]; then
  MODE="docker"
elif [[ "${1:-}" == "--host" ]]; then
  MODE="host"
elif [[ -n "${1:-}" ]]; then
  echo "Uso: $0 [--host|--docker]" >&2
  exit 1
fi

echo "==> [1/3] Verificando infraestructura base (Postgres Catalog + Garage S3)..."
COMPOSE=(docker compose)

if ! "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -q postgres-catalog; then
  echo "    Infraestructura detenida. Levantando servicios base..."
  "${COMPOSE[@]}" up -d postgres-catalog garage
fi

echo "    Asegurando que postgres-catalog y garage estén listos..."
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" exec -T postgres-catalog pg_isready -U "$CATALOG_USER" -d "$CATALOG_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if [[ "$MODE" == "docker" ]]; then
  echo "==> [2/3] Levantando servicio lake-api en Docker Compose..."
  "${COMPOSE[@]}" up -d --build lake-api

  echo "==> [3/3] Esperando verificación de salud en http://${API_HOST}:${API_PORT}/health..."
  for _ in $(seq 1 30); do
    if curl -s -f "http://${API_HOST}:${API_PORT}/health" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  echo ""
  echo "================================================================="
  echo "  Dulse Lake API & MCP corriendo en Docker (puerto ${API_PORT})"
  echo "================================================================="
  echo "  - Documentación Swagger UI: http://${API_HOST}:${API_PORT}/docs"
  echo "  - FastMCP SSE (Agentes AI): http://${API_HOST}:${API_PORT}/mcp/sse"
  echo "  - Auth (REST y MCP):       ${AUTH_HINT}"
  echo "  - Ver logs del contenedor:  docker compose logs -f lake-api"
  echo "  - Detener servicio:         docker compose stop lake-api"
  echo "================================================================="
  exit 0
fi

# Modo Host
echo "==> [2/3] Verificando entorno virtual de api (api/.venv)..."
if [[ ! -f "api/.venv/bin/python" ]]; then
  echo "    Creando entorno virtual con uv..."
  uv venv api/.venv --python 3.12
  uv pip install -p api/.venv/bin/python -r api/requirements.txt
fi

echo "==> [3/3] Iniciando servidor Uvicorn en http://${API_BIND_HOST}:${API_PORT}..."
echo ""
echo "================================================================="
echo "  Dulse Lake API & MCP corriendo en Host (puerto ${API_PORT})"
echo "================================================================="
echo "  - Documentación Swagger UI: http://${API_HOST}:${API_PORT}/docs"
echo "  - FastMCP SSE (Agentes AI): http://${API_HOST}:${API_PORT}/mcp/sse"
echo "  - FastMCP Stdio (Claude):   api/.venv/bin/python -m api.mcp_server (sin auth: proceso local)"
echo "  - Auth (REST y MCP):       ${AUTH_HINT}"
echo "================================================================="
echo ""

exec api/.venv/bin/uvicorn api.main:app --host "$API_BIND_HOST" --port "$API_PORT" --reload
