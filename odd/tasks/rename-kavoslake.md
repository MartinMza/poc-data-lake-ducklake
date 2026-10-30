# rename-kavoslake

Renombrar el proyecto de Dulse a KavosLake, incluyendo migración de datos.
Rama: `refactor/rename-kavoslake` (stacked sobre `refactor/hexagonal-architecture` / PR #3).

## Specs

- **S1** — "Quiero que me cambies el nombre del proyecto a un nombre más generico, no Dulse que no significa nada. Quiero un nombre mejor"
- **S2** — Nombre elegido por el usuario: "Mejor que se llame KavosLake"
- **S3** — Alcance elegido: "Rename completo con migración de datos" — incluidos `AUTH_DB_NAME`, cookie, issuer JWT, prefijo de keys, contenedores.
- **S4** — Convenciones: paquete `kavos_lake`, distribución `kavos-lake`, marca `KavosLake`, DB `kavos_lake_auth` (`_test` en tests), cookie `kavos_lake_session`, issuer `kavos-lake-api`, prefijo `kavos_lake_`, MCP `kavos-lake`, env `KAVOS_LAKE_ALLOW_PURGE`.
- **S5** — No-regresión: los 191 tests siguen verdes; el contrato HTTP no cambia.

## Tasks

- [ ] **T1** (S2,S4) — Paquete: `src/dulse` → `src/kavos_lake`, imports, pyproject, tests.
- [ ] **T2** (S3,S4) — Identificadores de runtime: DB, cookie, issuer, prefijo de keys, MCP name, env de purga.
- [ ] **T3** (S3) — Contenedores, imágenes y compose.
- [ ] **T4** (S3) — Migración de datos: script idempotente `ALTER DATABASE` + guía de operador.
- [ ] **T5** (S2) — Docs, títulos, `.env.example`, README.
- [ ] **T6** — Verificación: suite, docker build, grep de residuos.

## Log

- **L1** (verbatim): "Quiero que me cambies el nombre del proyecto a un nombre mas generico, no Dulse que no significa nada. Quiero un nombre mejor"
- **L2** — Aclaración: "Dulse" sí es una palabra (alga roja); el usuario prefiere un nombre anclado al dominio.
- **L3** — Dirección pedida: "Algo referido a datos e ai". Nombre final: "Mejor que se llame KavosLake".
- **L4** — Alcance: rename completo con migración de datos (elegido explícitamente).
- **L5** — La carpeta del repo (`.../dulse`) y `.env` están fuera del alcance de mis escrituras: el usuario debe renombrar la carpeta y editar `AUTH_DB_NAME`.
