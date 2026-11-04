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

echo
if (( FALLAS == 0 )); then
  printf '\033[92mTodo en orden.\033[0m\n'
  exit 0
fi
printf '\033[91m%d verificación(es) fallaron.\033[0m\n' "$FALLAS"
exit 1
