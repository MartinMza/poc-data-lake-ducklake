# hexagonal-refactor

Refactor estructural de Dulse: settings tipados, layout por capas y puertos/adaptadores reales.
Rama: `refactor/hexagonal-architecture` (base `origin/main` @ `1f76539`). Driver: preparación para producción.

## Specs

- **S1** — "Utilizar pydantic-settings para manejo de variables."
- **S2** — "Ordenar las carpetas. Usar un patron clean architecture"
- **S3** — "Usar arquitectura hexagonal"
- **S4** — Alcance elegido: "Big-bang: mover todo en una rama". Driver: "Preparar producción: más adapters reales".
- **S5** — No-regresión: los 178 tests existentes siguen verdes; el contrato HTTP (REST, panel, MCP en /mcp) no cambia; los entrypoints (`uvicorn api.main:app`, `python -m api.mcp_server`, docker, scripts) no se rompen.

## Tasks

- [x] **T1** (S2,S3) — pyproject + `src/`, paquete `dulse`, `src` en sys.path, `src` en la imagen — `c787169`
- [x] **T5+T6+T7** (S2,S3) — módulos movidos a `src/dulse` + shims `api/*` — `10e4ee5`
- [x] **T2** (S1) — `Settings` pydantic-settings con validación fail-closed — `179f158`
- [x] **T3** (S3) — `domain/sql_policy.py` (engine 476 → 346 líneas) — `8cfe8bc`
- [x] **T4** (S3) — puertos + test de conformidad — `a0cd4c8`
- [x] **T8** (S3) — test de regla de dependencia — `ababde6`
- [x] **T9** (S2) — `docs/ARCHITECTURE.md` + README — `9fc0a03`
- [x] **T10** (S5) — fixes de la verificación independiente + tests de regresión — `aba76d1`

## Slices de entrega (para el PR)

Los 8 commits son la unidad de review; no hay un commit gigante:

| Commit | Contenido | Review |
|---|---|---|
| `c787169` | fundación (pyproject, src, Docker) | mecánico |
| `10e4ee5` | movimiento de módulos + shims | el más grande; leer con `git show --stat` |
| `179f158` | pydantic-settings | funcional |
| `8cfe8bc` | política SQL al dominio | funcional |
| `a0cd4c8` | puertos | contratos |
| `ababde6` | regla de dependencia | test |
| `9fc0a03` | documentación | docs |
| `aba76d1` | fixes de verificación | correctivo |

## Evidencia

- Suite: **178 → 201 passed** (+5 settings, +3 puertos, +2 arquitectura, +13 regresión).
- Verificación independiente (`gentle-ai-verify`), dos rondas: primera **FAIL** con 2 blockers reales; tras corregirlos, segunda ronda **PASS**.
- Entrypoints verificados en runtime: `api.main:app` resuelve a FastAPI; `python -m api.mcp_server` arranca el servidor stdio (antes no-op silencioso); `scripts/hash_password.py` funciona.
- 0 imports `api.*` en todo `src/` (verificado con grep y con test).

## Verificación de entrega

- **Build de la imagen Docker**: verificado (`dulse-lake-api:refactor-verify`). Dentro de la imagen: `api.main:app` es FastAPI, 10 templates y `htmx.min.js` presentes, política SQL cargada.
- **`dulse.*` sin el shim**: verificado con `PYTHONPATH=/app/src`; ya no depende del efecto colateral del shim.
- **Pendiente (fuera de alcance)**: arranque real contra infraestructura (Postgres del catálogo + Garage).

## Log

- **L1** — Pedido original (verbatim):
  "1- Utilizar pydantic-settings para manejo de variables.
  2- Ordenar las carpetas. Usar un patron clean architecture
  3- Usar arquitectura hexagonal"
- **L2** — La decisión previa en memoria (no migrar a pydantic-settings) queda sobrescrita explícitamente por el usuario.
- **L3** — El usuario elige big-bang y driver producción. Se acuerda: big-bang en la rama, con commits por etapa, verdes.
- **L4** — No se tocan `feat/api-auth-mssql-only` ni `main` (pedido previo: "dejar como estan las ramas").
- **L5** — La rama se creó sobre `feat/brand-name-from-env` == `origin/main`; el "sin mergear" que reportó git era por un `main` local viejo (`11e7c48`).
- **L6** — Sin `pip`/setuptools en el venv: no se instala el paquete; `src` se resuelve por `sys.path` y se copia en la imagen.
- **L7** — Verificación independiente 1: FAIL (stdio no-op con `-m`; `security.py` importaba el shim). Corregido en `aba76d1` con tests de regresión.
- **L8** — Verificación independiente 2: PASS, 201 tests.
- **L9** — Build de la imagen verificado. El smoke test reveló que `dulse` sólo era importable por el shim: se agregó `PYTHONPATH=/app/src` y se re-verificó.
