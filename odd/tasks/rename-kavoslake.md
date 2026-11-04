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

- [x] **T1** (S2,S4) — Paquete: `src/dulse` → `src/kavos_lake`, imports, pyproject, tests — `310885d`
- [x] **T2** (S3,S4) — Identificadores de runtime: DB, cookie, issuer, prefijo, MCP name, env de purga — `a12e49e`
- [x] **T3** (S3) — Contenedores, imágenes y compose — `a12e49e`, normalizado en `6a2f848`
- [x] **T4** (S3) — Script de migración idempotente + guía de operador — `a12e49e`
- [x] **T5** (S2) — Docs, títulos, README, MANUAL — `231dde8`; **`.env.example` pendiente del usuario** (bloqueado por política)
- [x] **T6** — Verificación: suite, docker build, grep, verificación independiente (2 rondas)
- [x] **T7** — Test de regresión del rename (5 tests)

## Evidencia

- Suite: **191 → 196 passed**.
- Verificación independiente 1: **FAIL** — encontró nombres malformados (`kavos_lake-lake-pipeline`) y el `.env.example` viejo.
- Verificación independiente 2: **FAIL sólo por `.env.example`** (bloqueado para el agente); el resto PASS.
- Imagen: `kavos-lake-api:rename` construida; CMD apunta a `kavos_lake.adapters.inbound.http.api:app`; `import dulse` falla (nombre viejo eliminado); templates presentes; sin `/app/api`.
- Migración: `bash -n` OK, dry-run no muta, argumento inválido → exit 2.

## Bloqueado (requiere al usuario)

`.env.example`, líneas 34 y 40: `dulse_auth` → `kavos_lake_auth` (dos líneas). Sin eso, un clon nuevo (`setup.sh` copia ese archivo) apunta a la base vieja.
También: la carpeta del repo sigue llamándose `dulse` y el `.env` local conserva `AUTH_DB_NAME`/password viejos.

## Log

- **L1** (verbatim): "Quiero que me cambies el nombre del proyecto a un nombre mas generico, no Dulse que no significa nada. Quiero un nombre mejor"
- **L2** — Aclaración: "Dulse" sí es una palabra (alga roja); el usuario prefiere un nombre anclado al dominio.
- **L3** — Dirección pedida: "Algo referido a datos e ai". Nombre final: "Mejor que se llame KavosLake".
- **L4** — Alcance: rename completo con migración de datos (elegido explícitamente).
- **L5** — La carpeta del repo (`.../dulse`) y `.env` están fuera del alcance de mis escrituras: el usuario debe renombrar la carpeta y editar `AUTH_DB_NAME`.
