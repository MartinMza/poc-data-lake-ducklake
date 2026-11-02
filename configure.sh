#!/usr/bin/env bash
# configure.sh — Asistente amigable de configuración de entorno (.env) para KavosLake
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$ROOT/scripts/configure_env.py" "$@"
