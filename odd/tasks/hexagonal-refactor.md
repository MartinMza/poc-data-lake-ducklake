# hexagonal-refactor

Refactor estructural de Dulse: settings tipados, layout por capas y puertos/adaptadores reales.
Rama: `refactor/hexagonal-architecture`. Driver: preparación para producción (más adapters reales).

## Specs

- **S1** — "Utilizar pydantic-settings para manejo de variables."
- **S2** — "Ordenar las carpetas. Usar un patron clean architecture"
- **S3** — "Usar arquitectura hexagonal"
- **S4** — Alcance elegido por el usuario: "Big-bang: mover todo en una rama". Driver: "Preparar producción: más adapters reales".
- **S5** — Restricciones de no-regresión: los 178 tests existentes deben seguir verdes y el contrato HTTP público (rutas REST, panel HTMX, sub-app MCP en /mcp) no cambia. Los entrypoints (`uvicorn api.main:app`, docker-compose, docker/, scripts/) no se rompen.

## Tasks

- [ ] **T1** (S2,S3) — Fundación: `pyproject.toml` con layout `src/`, paquete `src/dulse/`, install editable, `conftest` con `src` en `sys.path`. Commit: `chore(arch): adopt src layout with pyproject`
- [ ] **T2** (S1) — pydantic-settings: `infrastructure/config.py` con `Settings(BaseSettings)`, `get_settings()` cacheado y accessors actuales preservados; fixture autouse que limpia cache. Commit: `refactor(config): typed settings with pydantic-settings`
- [ ] **T3** (S3) — Dominio puro: `domain/sql_policy.py` (validate_sql + constante de funciones prohibidas, sin importar duckdb) y `domain/models.py`. Commit: `refactor(domain): extract sql policy out of the engine`
- [ ] **T4** (S3) — Puertos: `Protocol`s para engine, repos de identidad, throttle y clock. Commit: `feat(ports): declare outbound ports`
- [ ] **T5** (S3) — Adapters de salida: `duckdb_engine`, `mssql_auth`, `memory_throttle` implementan los puertos. Commit: `refactor(adapters): implement outbound ports`
- [ ] **T6** (S2,S3) — Adapters de entrada: `http/api`, `http/panel`, `mcp/server`, `app.py` como composition root, templates movidos. Commit: `refactor(adapters): move inbound http and mcp`
- [ ] **T7** (S2) — Shims de compatibilidad en `api/*` re-exportando; entrypoints sin cambios. Commit: `refactor(api): keep compatibility shims`
- [ ] **T8** (S3) — Regla de dependencia como test (dominio/aplicación no importan fastapi/duckdb/pyodbc). Commit: `test(arch): enforce dependency rule`

## Log

- **L1** — Pedido original del usuario (verbatim):
  "1- Utilizar pydantic-settings para manejo de variables.
  2- Ordenar las carpetas. Usar un patron clean architecture
  3- Usar arquitectura hexagonal"
- **L2** — Verificación de tamaño: decisión previa en memoria decía NO migrar a pydantic-settings (config lee env por llamada, 9 tests dependen de monkeypatch.setenv). El usuario la sobrescribe explícitamente.
- **L3** — El usuario elige big-bang y driver producción al preguntársele el alcance y el objetivo. Se acuerda: big-bang en la rama, pero con commits por etapa, verdes, para review y rollback.
- **L4** — No se tocan `feat/api-auth-mssql-only` ni `main` (pedido previo del usuario: "dejar como estan las ramas").
