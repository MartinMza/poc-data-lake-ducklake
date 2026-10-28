# Dulse Lake — API REST (FastAPI) & Servidor MCP (FastMCP)

Capa de servicio para consultar el Data Lake DuckLake, que hoy contiene
**únicamente la data cargada desde SQL Server** (esquema `bronze`: 7 tablas
`mssql_*`) más los metadatos internos de dlt. No hay capas dbt (silver/gold)
ni origen sintético.

Ambas interfaces (HTTP REST y MCP) comparten el mismo motor DuckDB y la misma
autenticación.

---

## 1. Inicio rápido

```bash
./scripts/run_api.sh            # modo host con recarga en vivo
./scripts/run_api.sh --docker   # contenedor lake-api del compose
```

URLs:

- **Swagger UI**: <http://localhost:8000/docs>
- **Healthcheck (público)**: <http://localhost:8000/health>
- **MCP SSE**: `http://localhost:8000/mcp/sse`

`run_api.sh` valida la configuración de auth antes de arrancar y aborta con
instrucciones si falta (fail-closed).

Manual en el host:

```bash
uv venv api/.venv --python 3.12
uv pip install -p api/.venv/bin/python -r api/requirements.txt
api/.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 2. Autenticación (obligatoria)

Dos credenciales equivalentes; cualquiera de las dos abre todas las rutas
protegidas, tanto REST como MCP por HTTP/SSE:

| Credencial | Header | Pensada para |
|---|---|---|
| API key estática | `X-API-Key: <key>` | agentes, dashboards, clientes MCP, scripts |
| JWT (HS256) | `Authorization: Bearer <token>` | personas (Swagger incluido) |

Configuración (`.env` o entorno):

| Variable | Default | Descripción |
|---|---|---|
| `AUTH_API_KEYS` | *(vacío)* | keys separadas por comas; comparación constant-time |
| `AUTH_USERS` | *(vacío)* | `usuario:secreto,usuario2:secreto`; secreto plano o PBKDF2 |
| `AUTH_JWT_SECRET` | *(vacío)* | secreto HS256 (32+ bytes: `openssl rand -hex 32`) |
| `AUTH_JWT_TTL_MINUTES` | `60` | vigencia del access token |
| `AUTH_JWT_ISSUER` | `dulse-lake-api` | claim `iss` exigido |
| `CORS_ALLOW_ORIGINS` | local (`http://localhost:8000`, `http://127.0.0.1:8000`) | orígenes del navegador, lista por comas o `*` |
| `CORS_ALLOW_CREDENTIALS` | `false` | con `*` en orígenes se ignora y queda `false` |
| `SESSION_COOKIE_SECURE` | `false` | flag `Secure` de la cookie del panel (poné `true` con TLS) |
| `AUTH_LOGIN_MAX_ATTEMPTS` | `5` | fallos por usuario antes del bloqueo |
| `AUTH_LOGIN_WINDOW_SECONDS` | `300` | ventana deslizante de conteo |
| `AUTH_LOGIN_LOCKOUT_SECONDS` | `300` | duración del bloqueo |

Reglas:

- **Fail-closed**: sin `AUTH_API_KEYS` ni `AUTH_USERS` el servicio no arranca;
  con usuarios y sin `AUTH_JWT_SECRET` tampoco.
- Públicas solo `/health`, `/`, `POST /auth/token`, `/auth/panel` y `/static`.
  Todo lo demás (incluido `/mcp`) exige credencial.
- `/docs`, `/redoc` y `/openapi.json` piden **sesión del panel**: sin cookie
  redirigen a `/auth/panel/entrar?next=<ruta>`.
- Preflight CORS (`OPTIONS`) pasa sin credenciales.
- CORS por entorno (`CORS_ALLOW_ORIGINS`): el default es local y acotado. `*`
  con credenciales es inseguro y se rechaza (se desactivan las credenciales).
- Login con **rate-limiting** por usuario: tras `AUTH_LOGIN_MAX_ATTEMPTS` fallos
  dentro de `AUTH_LOGIN_WINDOW_SECONDS`, responde `429` con `Retry-After`
  durante `AUTH_LOGIN_LOCKOUT_SECONDS`. Es por-proceso (un worker); con varios
  workers hace falta un store compartido. El login es constant-time para no
  filtrar qué usuarios existen.
- El transporte MCP por **stdio** (`python -m api.mcp_server`) corre como
  proceso local del operador y no pasa por esta autenticación.

### API key

```bash
curl -s -H "X-API-Key: $AUTH_API_KEYS" http://localhost:8000/api/v1/sqlserver/tables | head
```

### JWT

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<password>"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/tables
```

Generar el hash de un password para `AUTH_USERS`:

```bash
api/.venv/bin/python scripts/hash_password.py
# → pbkdf2_sha256$260000$<salt>$<hash>
```

En Swagger UI: botón **Authorize** → esquema `ApiKeyAuth` (`X-API-Key`) o
`BearerAuth` (JWT).

---

## 3. Endpoints REST

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/auth/token` | Emite un JWT para un usuario de `AUTH_USERS` (público). |
| `GET` | `/health` | Healthcheck (público). |
| `GET` | `/api/v1/tables` | Tablas del lago con conteo de filas (`?schemas=bronze`). |
| `GET` | `/api/v1/tables/{schema}/{table}` | Columnas, tipos, total de filas y muestra de 5 registros. |
| `GET` | `/api/v1/sqlserver/tables` | **Inventario de lo cargado desde SQL Server**: filas, columnas, columnas y `last_load_id` (frescura). |
| `GET` | `/api/v1/sqlserver/tables/{table}/rows` | Página de filas reales (`?limit=20&offset=0`, máx. 100). |
| `GET` | `/api/v1/snapshots` | Snapshots del catálogo DuckLake (auditoría y Time Travel). |
| `POST` | `/api/v1/query` | SQL analítico libre con guardrails (`{"sql": "...", "limit": 100}`). |

Ejemplo:

```bash
curl -s -H "X-API-Key: $AUTH_API_KEYS" \
  "http://localhost:8000/api/v1/sqlserver/tables/mssql_bs_moneda/rows?limit=3"
```

```bash
curl -s -X POST "http://localhost:8000/api/v1/query" \
  -H "Content-Type: application/json" -H "X-API-Key: $AUTH_API_KEYS" \
  -d '{"sql":"SELECT COUNT(*) AS n FROM lake.bronze.mssql_bs_transaccion","limit":10}'
```

> Nota: el fast path de ingesta (`bronze/fast_mssql.py`) normaliza los nombres
> de columna a `snake_case` (`transaccion_id`, `importe_total`, …), por eso la
> API y el MCP los exponen así.

---

## 4. Servidor MCP (FastMCP)

Herramientas expuestas:

1. `sqlserver_tables()` — inventario de las 7 tablas de SQL Server con filas,
   columnas y `last_load_id`.
2. `list_tables(schemas=None)` — tablas del lago (default `bronze`).
3. `describe_table(schema, table)` — columnas, tipos y muestra.
4. `preview_rows(table, limit=20, offset=0, schema="bronze")` — página de
   filas reales, sin escribir SQL.
5. `run_query(sql, limit=100)` — SQL analítico (soporta Time Travel
   `AT (VERSION => N)`).
6. `list_snapshots(limit=10)` — snapshots del catálogo.

Recursos: `lake://catalog` (inventario del lago) y `lake://sqlserver/tables`
(inventario SQL Server con marca de última carga).

### Claude Desktop / stdio (sin auth, proceso local)

```json
{
  "mcpServers": {
    "dulse-datalake": {
      "command": "/home/mmediza/orca/workspaces/POC-Data-Lake/dulse/api/.venv/bin/python",
      "args": ["-m", "api.mcp_server"],
      "env": {
        "CATALOG_HOST": "localhost",
        "CATALOG_PORT": "5433",
        "GARAGE_S3_ENDPOINT": "http://localhost:3900"
      }
    }
  }
}
```

### Clientes HTTP / SSE (con auth)

Apuntá el cliente a `http://localhost:8000/mcp/sse` y enviá el header
`X-API-Key: <key>` (o `Authorization: Bearer <jwt>`) en todas las requests.
Sin credencial el endpoint responde `401`.

---

## 5. Guardrails del SQL libre

- **Solo lectura**: la sentencia debe empezar con `SELECT`, `WITH`,
  `DESCRIBE`, `SUMMARIZE` o `EXPLAIN`. DDL/DML (`DROP`, `DELETE`, `UPDATE`,
  `INSERT`, `ALTER`, `TRUNCATE`, `CREATE`, `ATTACH`, …) → `400`.
- **Sin múltiples sentencias**: cualquier `;` intermedio se rechaza.
- **Tope de filas**: `limit` acepta 1–1000 (más que eso → `422`); el motor
  aplica el tope al envolver la consulta.

---

## 6. Tests

```bash
api/.venv/bin/pytest -v api/
```

- `api/test_auth.py` — API key, JWT, expiración, firma inválida, rutas
  públicas, fail-closed por falta de configuración.
- `api/test_api_mcp.py` — catálogo bronze-only, inventario SQL Server,
  paginado, guardrails y tools/recursos del MCP (requiere el stack arriba con
  la data de SQL Server ya ingestada).
