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

- [x] **T1** (S2,S3) — pyproject + layout `src/`, paquete `dulse`, `src` en `sys.path`, `src` copiado en la imagen — `c787169`
- [x] **T5+T6+T7** (S2,S3) — módulos movidos a `src/dulse` (infrastructure, adapters inbound/outbound, templates, static) + shims `api/*` — `10e4ee5`
- [x] **T2** (S1) — `Settings` con pydantic-settings, validación fail-closed, resolver genérico con el mismo parser `.env` — `179f158`
- [x] **T3** (S3) — `domain/sql_policy.py`: política del sandbox fuera del adaptador (476 → 346 líneas el engine) — `8cfe8bc`
- [x] **T4** (S3) — puertos `QueryEnginePort` + identidad + test de conformidad de adaptadores — `a0cd4c8`
- [x] **T8** (S3) — test de regla de dependencia (dominio no importa frameworks/adapters/infrastructure) — `ababde6`
- [x] **T9** (S2) — `docs/ARCHITECTURE.md` + README + este documento — commit final

## Evidencia

- Suite: **178 → 188 passed** (se sumaron 5 de settings, 3 de puertos, 2 de arquitectura).
- Entrypoints verificados por smoke test: `api.main.app` es FastAPI, `api.mcp_server` es el módulo real, el singleton del throttle es el mismo objeto.
- 0 imports `api.*` remanentes dentro de `src/`.

## Log

- **L1** — Pedido original (verbatim):
  "1- Utilizar pydantic-settings para manejo de variables.
  2- Ordenar las carpetas. Usar un patron clean architecture
  3- Usar arquitectura hexagonal"
- **L2** — La decisión previa en memoria (no migrar a pydantic-settings) queda sobrescrita explícitamente por el usuario.
- **L3** — El usuario elige big-bang y driver producción. Se acuerda: big-bang en la rama, con commits por etapa, verdes.
- **L4** — No se tocan `feat/api-auth-mssql-only` ni `main` (pedido previo: "dejar como estan las ramas").
- **L5** — La rama se creó sobre `feat/brand-name-from-env` == `origin/main`; el "sin mergear" que reportó git era por un `main` local viejo (`11e7c48`), no por trabajo pendiente.
- **L6** — Sin `pip`/setuptools en el venv: no se instala el paquete, se resuelve `src` por `sys.path` (conftest + shims) y se copia `src/` en la imagen Docker.
