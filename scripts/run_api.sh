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
  if "${COMPOSE[@]}" exec -T postgres-catalog pg_isready -U ducklake -d ducklake_catalog >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if [[ "$MODE" == "docker" ]]; then
  echo "==> [2/3] Levantando servicio lake-api en Docker Compose..."
  "${COMPOSE[@]}" up -d --build lake-api

  echo "==> [3/3] Esperando verificación de salud en http://localhost:8000/health..."
  for _ in $(seq 1 30); do
    if curl -s -f "http://localhost:8000/health" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  echo ""
  echo "================================================================="
  echo "  Dulse Lake API & MCP corriendo en Docker (puerto 8000)"
  echo "================================================================="
  echo "  - Documentación Swagger UI: http://localhost:8000/docs"
  echo "  - FastMCP SSE (Agentes AI): http://localhost:8000/mcp/sse"
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

echo "==> [3/3] Iniciando servidor Uvicorn en http://0.0.0.0:8000..."
echo ""
echo "================================================================="
echo "  Dulse Lake API & MCP corriendo en Host (puerto 8000)"
echo "================================================================="
echo "  - Documentación Swagger UI: http://localhost:8000/docs"
echo "  - FastMCP SSE (Agentes AI): http://localhost:8000/mcp/sse"
echo "  - FastMCP Stdio (Claude):   api/.venv/bin/python -m api.mcp_server"
echo "================================================================="
echo ""

exec api/.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
