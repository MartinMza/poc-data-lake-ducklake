# Auth UI: superAdmin, usuarios y API keys en Postgres

Feature: panel web mínimo para gestionar credenciales, con las credenciales
viviendo en Postgres en lugar del `.env`. Reemplaza el flujo actual de
"editar `.env` a mano" por una UI de tres pantallas.

## Specs

- **S1** — "un front simple, donde la primera vez va a pedir la contraseña del
  superAdmin". El primer arranque, sin superAdmin en la base, expone un wizard
  que exige un **token de setup de un solo uso** impreso en el log del servicio
  (decisión D2). Sin token no se crea nada.
- **S2** — "Ahi va a poder agregar nuevos usuarios". El superAdmin crea usuarios
  con usuario y password; el password se hashea antes de guardarse (S6).
- **S3** — "Los nuevos usuarios van a poder crear y rotar sus api keys."
  Cada usuario gestiona sus propias keys, y sólo las propias.
- **S4** — "Como máximo van a poder tener 5 keys consecutivas." Se permiten como
  máximo **5 API keys activas** (no vencidas y no revocadas) por usuario; la
  sexta se rechaza con un error explícito.
- **S5** — "cuando se crean van a poder seleccionar el vencimiento, el más largo
  son 180 días". Al crear una key se elige el vencimiento en días; el máximo
  aceptado es 180 y un valor mayor se rechaza.
- **S6** — "Re utilizar la base de postgress para guardar los usuarios y demas
  informacion necesaria." Base `dulse_auth` en el mismo `postgres-catalog`
  (decisión D1), sin tocar el esquema del catálogo DuckLake.
- **S7** — "Importante que las password esten hasheadas en la db": PBKDF2-SHA256
  con salt por usuario, el mismo formato que ya usa `api/auth.py`. En la base no
  hay passwords en claro ni API keys en claro: de la key se guarda SHA-256 más
  un prefijo visible (decisión D3).
- **S8** — "la `AUTH_API_KEYS` del `.env` … se elimina: todo pasa a la DB"
  (decisión D4). La migración es de una sola vez: si el entorno trae
  `AUTH_API_KEYS`/`AUTH_USERS`, se importan a la base en el primer arranque y el
  `.env` queda como legado que ya no se lee.

## Tasks

| ID | Spec | Qué | Commit |
|---|---|---|---|
| T1 | S6 | Base `dulse_auth` + esquema idempotente (`users`, `api_keys`, `sessions`, `setup_tokens`) y módulo `api/auth_db.py` | C1 |
| T2 | S7 | Hash de password y de API key; prefijo visible; helpers de creación/verificación | C1 |
| T3 | S1 | Token de setup de un solo uso al arrancar sin superAdmin | C2 |
| T4 | S1,S2 | Endpoints de sesión: setup, login, logout, me | C2 |
| T5 | S2,S3 | Endpoints de usuarios (sólo superAdmin) y de keys propias | C2 |
| T6 | S8 | Middleware contra la base: API key por hash, JWT desde la fila del usuario, migración one-shot del `.env` | C3 |
| T7 | S1,S8 | Front `api/static/authui.html` servido en `/ui`, sin build | C4 |
| T8 | S4,S5 | Reglas: máximo 5 keys activas, vencimiento ≤ 180 días, revocación | C2 |
| T9 | — | Tests de todas las reglas por la interfaz pública | C1-C4 |
| T10 | — | Docs: MANUAL, README, api/README, `.env.example`, compose | C4 |

## Log

- **L1** (pedido original, verbatim): "Me parece un poco complejo y poco amigable
  para las personas que lo van a usar. Necesito que me crees un front simple,
  donde la primera vez va a pedir la contraseña del superAdmin. Ahi va a poder
  agregar nuevos usuarios. Los nuevos usuarios van a poder crear y rotar sus api
  keys. Como máximo van a poder tener 5 keys consecutivas, y cuando se crean van
  a poder seleccionar el vencimiento, el más largo son 180 días. Re utilizar la
  base de postgress para guardar los usuarios y demas informacion necesaria.
  Importante que las password esten hasheadas en la db"
- **L2** (decisiones, elegidas por el usuario): D1 base `dulse_auth` en el mismo
  Postgres · D2 token de setup de un solo uso + wizard · D3 sólo hash, la key se
  muestra una vez · D4 se elimina `AUTH_API_KEYS` del entorno.

## Estado de ejecución

| Task | Estado | Evidencia |
|---|---|---|
| T1 esquema + `auth_db` | hecho | C1 (`04bb442`), 15 tests contra Postgres real |
| T2 hash de password y key | hecho | C1: PBKDF2 + SHA-256 + prefijo visible |
| T3 token de setup | hecho | C2: impreso en el log del arranque, un solo uso, 30 min |
| T4 sesión (setup/login/logout/me) | hecho | C2: cookie HttpOnly, 8 h |
| T5 usuarios y keys | hecho | C2: alta/baja de usuarios, crear/rotar/revocar keys |
| T6 middleware contra la base | hecho | C2: API key por hash con cache de 5 s, JWT desde la fila del usuario |
| T7 front `/ui` | hecho | C2: página única sin build |
| T8 reglas 5 keys / 180 días | hecho | C2: validadas en `auth_db`, probadas por HTTP |
| T9 tests | hecho | 68 en total; el panel tiene 15 tests propios |
| T10 docs | hecho | `docs/MANUAL.md` (panel + recetas), `.env.example`, compose, scripts |

Bug encontrado por la verificación en vivo: `authenticate_api_key` exigía el
prefijo `dulse_`, así que las keys migradas del `.env` daban 401. Test primero
(`test_key_migrada_sin_prefijo_tambien_autentica`), después el fix.
