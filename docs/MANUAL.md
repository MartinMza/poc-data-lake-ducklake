# Manual del Data Lake — cómo funciona y cómo se opera

> Pensado para leerse en pedazos cortos. Cada bloque trae un comando y lo que
> tenés que ver para saber que salió bien. Si algo no coincide, andá directo a
> [Cuando algo falla](#cuando-algo-falla) y no leas el resto.

**Qué es esto**: un lakehouse local que copia 7 tablas de SQL Server a un Data
Lake con formato abierto (DuckLake sobre Parquet en S3), y las expone por API
REST y por MCP. No hay datos generados: lo que ves es lo que vino de SQL Server.

---

## Si solo tenés 5 minutos

Tres comandos. Si los tres funcionan, el sistema está sano.

```bash
docker compose ps --format '{{.Service}}\t{{.Status}}'     # 1. ¿está arriba?
bronze/.venv/bin/python scripts/lake_counts.py             # 2. ¿hay datos?
api/.venv/bin/python -m pytest -q api/                     # 3. ¿anda todo?
```

Qué esperar:

1. `mssql`, `postgres-catalog`, `garage`, `prefect-server` en `Up (healthy)`. La API puede o no estar: se levanta aparte.
2. Las **7 tablas `mssql_*`** y ~485.557 filas en total.
3. `39 passed` en ~5 segundos.

Si los tres dan bien, podés saltear el resto del manual y volver cuando
necesites tocar algo.

---

## El mapa mental (una sola imagen)

```
   .bak de SQL Server (7,5 GB)
            │
            │  restore_mssql.sh
            ▼
      SQL Server (mssql:1433)
            │
            │  bronze/fast_mssql.py  (bcp → staging → MERGE)
            ▼
   DuckLake: Parquet en Garage S3 + metadatos en Postgres
            │
            │  motor DuckDB de sólo lectura
            ├──────────────► API REST   (localhost:8000)
            └──────────────► MCP        (localhost:8000/mcp)
```

Tres ideas que explican todo lo demás:

- **Un solo camino de datos**: `.bak` → SQL Server → `bronze`. No hay capas
  intermedias ni transformaciones de negocio.
- **Dos bases distintas**: Postgres guarda *metadatos* del catálogo (qué tablas
  y archivos existen), no los datos. Los datos son Parquet en Garage.
- **Dos formas de entrar**: REST y MCP comparten el mismo motor y la misma
  autenticación.

---

## Cómo está armado

| Servicio | Puerto | Para qué sirve | Cómo saber si está sano |
|---|---|---|---|
| `mssql` | 1433 | el origen real de los datos | `docker compose ps mssql` → `healthy` |
| `postgres-catalog` | 5433 | metadatos de DuckLake | `healthy` |
| `garage` | 3900–3903 | S3 donde viven los Parquet | `healthy` |
| `prefect-server` | 4200 | UI/API de la orquestación | abrí http://localhost:4200 |
| `lake-api` | 8000 | API REST + MCP | http://localhost:8000/health → `200` |
| `lake-init` | — | job de una sola vez: restore + ingesta | corre y termina |

La API y el job de init usan los mismos puertos internos que el host: si algo
ya está escuchando en 8000, `lake-api` no arranca.

---

## Los datos: 7 tablas y nada más

| Tabla | Filas | Qué es |
|---|---|---|
| `bronze.mssql_bs_transaccion` | 320.447 | transacciones |
| `bronze.mssql_bs_operacion` | 148.356 | operaciones (170 columnas) |
| `bronze.mssql_bs_producto` | 9.342 | productos |
| `bronze.mssql_d_mart_ventas_producto_cliente` | 4.065 | ventas por producto/cliente |
| `bronze.mssql_bs_organizacion` | 3.055 | organizaciones |
| `bronze.mssql_d_mart_ventas` | 275 | resumen de ventas |
| `bronze.mssql_bs_moneda` | 17 | monedas |

Más tres tablas internas de `dlt` (`_dlt_loads`, `_dlt_pipeline_state`,
`_dlt_version`) que son bookkeeping: no las consultes, no son datos.

**Regla de nombres**: la ingesta rápida normaliza todo a `snake_case`
(`TransaccionID` → `transaccion_id`). Si buscás un nombre en CamelCase, no está.

---

## Cómo entrar a la data

### Autenticación: dos caminos, uno obligatorio

Todo pasa por credencial salvo `/health`, `/`, `/static`, `/auth/panel` y
`POST /auth/token`.

**La documentación (Swagger y `/openapi.json`) ya no es pública**: pide sesión
del panel. Si no estás logueado te manda a entrar y después te devuelve a donde
ibas.

| Quién sos | Qué usás | Cómo |
|---|---|---|
| script, agente, dashboard | **API key** | header `X-API-Key: <key>` (se crea en `/auth/panel`) |
| persona, Swagger, cliente MCP | **JWT** | `POST /auth/token` → `Authorization: Bearer <token>` |

Las credenciales viven en Postgres (base `dulse_auth`), no en el `.env`. Para
conseguir una API key: entrá al panel en **http://localhost:8000/auth/panel** y
creala ahí; se muestra una sola vez.

```bash
curl -s -H "X-API-Key: $KEY" localhost:8000/api/v1/sqlserver/tables
```

Es **fail-closed**: sin credenciales válidas ninguna request de datos pasa.

### API REST

| Endpoint | Qué devuelve |
|---|---|
| `GET /api/v1/sqlserver/tables` | inventario de las 7 tablas, con filas y columnas |
| `GET /api/v1/sqlserver/tables/{tabla}/rows?limit=N` | filas paginadas (`limit` ≤ 1000) |
| `GET /api/v1/tables` | catálogo observable del lake |
| `GET /api/v1/snapshots` | snapshots de DuckLake (time travel) |
| `POST /api/v1/query` | SQL libre, **sólo** `SELECT`/`WITH`/`DESCRIBE`/`SUMMARIZE`/`EXPLAIN` |

Dos defensas que conviene conocer:

- `limit` mayor a 1000 → **422**, no recorta en silencio.
- Cualquier otra sentencia → **400** con el motivo. `DROP`, `ATTACH`, `COPY`
  están bloqueados antes de ejecutarse.

### MCP

| Transporte | Cómo se autentica | Cuándo usarlo |
|---|---|---|
| HTTP/SSE en `http://localhost:8000/mcp/sse` | header `X-API-Key` o Bearer | agentes y clientes remotos |
| stdio: `python -m api.mcp_server` | **sin auth** (corre como tu proceso) | Claude Desktop y similares en tu máquina |

Herramientas: `sqlserver_tables`, `list_tables`, `describe_table`,
`preview_rows`, `run_query`, `list_snapshots`.
Recursos: `lake://catalog` y `lake://sqlserver/tables`.

El transporte stdio no pasa por auth porque ya tiene tus permisos de shell.
Eso es por diseño, no un olvido.

---

## Los comandos que vas a usar de verdad

| Quiero… | Comando | Ojo con |
|---|---|---|
| levantar todo de cero | `./scripts/setup.sh` | si hay `.bak`, lo restaura: son **minutos** |
| levantar sólo la API | `./scripts/run_api.sh --host` | valida auth antes de arrancar |
| levantar la API en Docker | `docker compose up -d lake-api` | no usar `up -d` a secas (ver trampas) |
| ver si hay datos | `bronze/.venv/bin/python scripts/lake_counts.py` | usa el venv de bronze |
| correr la orquestación | `orchestration/.venv/bin/python orchestration/flow.py` | necesita `PREFECT_API_URL` y `MSSQL_DATABASE` |
| restaurar el `.bak` | `./scripts/restore_mssql.sh <archivo>.bak FAF12_Kavos` | idempotente, usa `REPLACE` |
| limpiar datos sintéticos viejos | `bronze/.venv/bin/python scripts/purge_sample_data.py` | dry-run por defecto; `--apply` ejecuta |
| generar una API key | `openssl rand -hex 24` | 24 caracteres mínimo |
| generar un usuario JWT | `api/.venv/bin/python scripts/hash_password.py` | `--password-stdin` para automatizar |
| backup / restore del lake | `./scripts/backup.sh` / `./scripts/restore.sh <backup>` | el restore re-crea el catálogo |
| correr los tests | `api/.venv/bin/python -m pytest -q api/` | necesitan el stack arriba con datos |

El resto (`bootstrap.sh`, `smoke_ducklake.py`, `s3_sync.py`,
`timetravel_check.py`, `container_init.sh`) es infraestructura: se usa al
montar el stack o desde Docker, no en el día a día.

---

## Cuando algo falla

Buscá tu síntoma. Si no está acá, el log del servicio suele decir la verdad:
`docker compose logs --tail=30 <servicio>`.

| Síntoma | Causa | Solución |
|---|---|---|
| `postgres-source` sigue corriendo | quedó de una versión anterior del compose | `docker compose up -d --remove-orphans` |
| `docker compose up -d` se queda minutos | levantó `lake-init`, que restaura el `.bak` y reingesta | levantar servicio por servicio, o esperar |
| API no arranca: *API key demasiado corta* | key de menos de 24 caracteres | `openssl rand -hex 24` en `AUTH_API_KEYS` |
| API no arranca: *AUTH_USERS tiene una entrada inválida* | falta el `:` (`usuario:secreto`) | corregir la entrada, o borrarla |
| API no arranca: *Auth sin configurar* | faltan `AUTH_API_KEYS` y `AUTH_USERS` | `./scripts/setup.sh` los genera |
| 401 con una key que jura que es correcta | espacios o comas de más en `.env` | `grep '^AUTH_API_KEYS=' .env` y mirar el valor crudo |
| 422 al pedir filas | `limit` > 1000 | pedir ≤ 1000 y paginar |
| 404 en `/api/v1/kpis/*` | los KPIs se fueron con dbt | no existen más, usá `POST /api/v1/query` |
| 401 al loguear con password con `ñ` | el password no coincide | es correcto: credencial inválida, nunca un 500 |
| los tests fallan por datos faltantes | stack abajo o lake vacío | pasos 1, 2 y 3 de "5 minutos" |
| el `.bak` no aparece | está en `mssql-backup/` con otro nombre | `ls mssql-backup/` |

---

## Lo que ya no existe (no lo busques)

| Cosa | Estado |
|---|---|
| `dbt/` (silver, gold, modelos, tests) | eliminado: el lake es sólo `bronze` |
| `source/` (generador de datos sintéticos) | eliminado |
| `bronze/pipeline.py` (camino lento) | eliminado |
| `scripts/export_gold.py`, `benchmark.py`, `concurrency_check.py` | eliminados |
| servicio `postgres-source` y `SEED_SCALE` | eliminados del compose |
| `/api/v1/kpis/*` y las tablas `silver`/`gold` | eliminados con dbt |

Si algo te pide dbt o datos de ejemplo, está desactualizado: este repo ya no
los tiene.

---

## Reglas del repo

- **Secretos nunca en git**: `.env` está ignorado; `.env.example` es plantilla
  vacía. Si ves una credencial usable commiteada, es un bug.
- **`snake_case` en la data**: es lo que devuelve la ingesta.
- **Un commit por unidad revisable**: el detalle de cómo se trabajó está en
  `odd/tasks/api-auth-mssql-only.md`.
- **Los tests corren contra la data real**, no contra fixtures. Si el lake está
  vacío, los tests de API/MCP no pueden pasar.

---

## Dónde está cada cosa

| Archivo | Qué contiene |
|---|---|
| `README.md` | el quickstart general |
| `docs/MANUAL.md` | este manual |
| `docs/findings*.md` | decisiones, benchmarks y fricciones encontradas |
| `api/README.md` | detalle de REST + MCP y configuración de clientes |
| `bronze/README.md` | cómo ingesta `fast_mssql.py` |
| `orchestration/README.md` | el flow de Prefect |
| `scripts/README.md` | procedimiento de infraestructura |
| `odd/tasks/api-auth-mssql-only.md` | spec, tareas y registro de reviews |
| `docker-compose.yml` | servicios, puertos y variables |
| `.env.example` | todas las variables, sin valores secretos |

**Variables de entorno, por familia**: `AUTH_*` (credenciales), `CATALOG_*`
(metadatos), `GARAGE_*` (S3), `MSSQL_*` (origen), `API_*` (servidor),
`PREFECT_*` (orquestación).

---

## Si sos una sesión nueva (agente o persona)

Leé en este orden y pará cuando tengas lo que necesitás:

1. Este manual, la sección "5 minutos".
2. `api/README.md` si vas a tocar la API o el MCP.
3. `orchestration/README.md` si vas a tocar la ingesta.
4. `odd/tasks/api-auth-mssql-only.md` para saber qué se decidió y por qué.

Tres cosas que te van a ahorrar tiempo:

- Verificá primero con los tres comandos de "5 minutos". No arregles nada antes
  de saber qué está sano.
- No uses `docker compose up -d` a secas: levanta `lake-init` y dispara una
  reingesta.
- El rango de commits de la feature de auth está revisado y aprobado; si tocás
  `api/auth.py`, esperá otra revisión.

---

## Panel de acceso (`/auth/panel`)

Todo lo de credenciales se hace en el navegador: **http://localhost:8000/auth/panel**.
La ruta vieja `/ui` redirige sola.

Es HTML armado en el servidor (Jinja2); las tablas se refrescan con HTMX sin
recargar la página. No hay build, ni npm, ni CDN: `htmx.min.js` vive en
`api/static/`.

### Las vistas

| Vista | Ruta | Quién entra |
|---|---|---|
| Entrar | `/auth/panel/entrar` | todos |
| Documentación | `/docs` · `/redoc` | con sesión del panel |
| Mis API keys | `/auth/panel` | con sesión |
| Mi contraseña | `/auth/panel/password` | con sesión |
| Usuarios | `/auth/panel/usuarios` | sólo superAdmin |
| Activar cuenta | `/auth/panel/activar/<token>` | quien tenga el link |
| Primera configuración | `/auth/panel/configuracion` | sólo si no hay superAdmin |

### Qué se puede hacer

| Quiero… | Dónde | Nota |
|---|---|---|
| entrar | "Entrar" | usuario y contraseña |
| crear una API key | "Crear API key" | elegís para qué es y cuántos días |
| copiar una key nueva | banner verde | **se muestra una sola vez** |
| rotar una key | "Rotar" en la fila | revoca la vieja y crea la nueva |
| revocar una key | "Revocar" en la fila | el cliente que la use deja de funcionar |
| agregar un usuario | "Agregar usuario" | dos caminos: link o contraseña temporal |
| resetear una contraseña | "Link nuevo" / "Resetear" en la fila | sólo superAdmin |
| deshabilitar un usuario | "Deshabilitar" | corta sesión, keys y JWT |
| cambiar mi contraseña | "Mi contraseña" | cierra tus otras sesiones del panel |

### Cómo llega la primera contraseña

Nadie define la contraseña de otro. Al crear un usuario elegís uno de dos caminos:

| Camino | Qué recibe la persona | Qué pasa al entrar |
|---|---|---|
| **Link de activación** (recomendado) | una URL de un solo uso, válida 24 h | define su contraseña ahí mismo |
| **Contraseña temporal** | una clave aleatoria que ves una sola vez | el panel lo obliga a cambiarla antes de hacer cualquier otra cosa |

El link es de un solo uso: si se usa, vence o se emite uno nuevo, el anterior
deja de servir. Un usuario deshabilitado no puede usar el link.

### Reglas que aplica el servidor (no la UI)

- máximo **5 API keys activas** por usuario;
- vencimiento entre **1 y 180 días**;
- contraseña de al menos **12 caracteres** y distinta del nombre de usuario;
- el password se guarda hasheado (PBKDF2) y de cada key sólo queda el hash;
- los campos de contraseña tienen botón **Ver** para revisarlos antes de guardar;
- **resetear la contraseña corta las sesiones del panel del usuario, no sus API keys**;
- **deshabilitar** corta todo: sesión, keys y JWT en el acto.

### Primer arranque

Si la base no tiene ningún superAdmin, el log del servicio imprime un
**SETUP TOKEN** de un solo uso (vence en 30 minutos):

```bash
docker compose logs lake-api | grep -A1 "SETUP TOKEN"
```

Con ese token, `/auth/panel/configuracion` crea el superAdmin. Después esa
pantalla redirige al login.

### Si te olvidás el password del superAdmin

```bash
HASH=$(api/.venv/bin/python scripts/hash_password.py --password-stdin <<< 'nuevo-password')
docker compose exec -T postgres-catalog psql -U ducklake -d dulse_auth \
  -c "UPDATE users SET password_hash='$HASH' WHERE username='admin';"
```

### Empezar de cero con las credenciales

```bash
docker compose exec -T postgres-catalog psql -U ducklake -d postgres \
  -c "DROP DATABASE IF EXISTS dulse_auth"
docker compose restart lake-api      # recrea la base e imprime un SETUP TOKEN nuevo
```

Ojo: eso borra usuarios y keys. Los datos del lake no se tocan.

---

## Checklist de sistema sano

- [ ] Los 4 servicios base en `healthy`.
- [ ] `lake_counts.py` muestra 7 tablas `mssql_*` y ~485.557 filas.
- [ ] `/health` responde 200 sin credencial.
- [ ] `/api/v1/sqlserver/tables` responde 401 sin credencial y 200 con key.
- [ ] `pytest -q api/` da 39 passed.
- [ ] `docker compose ps` no muestra servicios huérfanos.
