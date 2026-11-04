# Arquitectura de KavosLake

Organización **hexagonal (puertos y adaptadores)** con la **regla de dependencia**
de clean architecture como invariante verificable.

## Capas

```
src/kavos_lake/
├── domain/         reglas puras: política SQL del sandbox (sin I/O, sin frameworks)
├── ports/          contratos (Protocol): motor de consultas e identidad
├── adapters/
│   ├── inbound/    lo que entra:  http/ (REST + panel HTMX), mcp/ (FastMCP)
│   └── outbound/   lo que sale:   duckdb_engine, postgres_auth, memory_throttle
├── infrastructure/ configuración (pydantic-settings) y seguridad (JWT, PBKDF2)
└── adapters/inbound/http/{templates,static}
```

## Regla de dependencia

`domain` no importa frameworks, drivers ni las capas `adapters`/`infrastructure`.
Lo verifica `tests/test_architecture.py`: si se cruza el borde, el build falla.

```
inbound ──▶ ports ◀── outbound
   │                     │
   └──▶ infrastructure ◀─┘
            ▲
         domain (nadie del dominio mira hacia arriba)
```

## Mapa de módulos

| Módulo | Qué es | Capa |
|---|---|---|
| `domain/sql_policy.py` | sentencias permitidas, keywords/funciones prohibidas, bloqueo de `://`, validación de identificadores | dominio |
| `ports/engine.py` | `QueryEnginePort` | puerto |
| `ports/identity.py` | usuarios, sesiones, API keys, throttle, clock | puerto |
| `adapters/outbound/duckdb_engine.py` | conexión DuckLake + consultas; aplica la política | adaptador de salida |
| `adapters/outbound/postgres_auth.py` | persistencia de credenciales (Postgres) | adaptador de salida |
| `adapters/outbound/memory_throttle.py` | rate-limit de login en memoria | adaptador de salida |
| `adapters/inbound/http/api.py` | app FastAPI, rutas REST, CORS, docs gate | adaptador de entrada |
| `adapters/inbound/http/panel.py` | panel server-side Jinja2 + HTMX | adaptador de entrada |
| `adapters/inbound/mcp/server.py` | herramientas y recursos FastMCP | adaptador de entrada |
| `infrastructure/config.py` | `Settings` (pydantic-settings) + resolución de `.env` | infraestructura |
| `infrastructure/security.py` | JWT HS256, PBKDF2, middleware de auth | infraestructura |

## Agregar código

- Una regla nueva sin I/O → `domain/`.
- Un proveedor nuevo (otra base, otro motor, otro store) → `adapters/outbound/` + un puerto en `ports/`.
- Una ruta o herramienta nueva → `adapters/inbound/`.
- Una variable de entorno nueva → campo tipado en `Settings` si su tipo importa;
  si no, `env()`.

## Orquestación (fuera del hexágono)

El flow de Prefect no es parte de `src/kavos_lake/` y la regla de dependencia
de arriba no lo cubre: es un deployable aparte que usa el lago como caja
negra (subprocesos + env, igual que un operador). El detalle del camino de
datos está en `orchestration/README.md`; acá sólo el mapa para ubicarse:

```
orchestration/
├── flow.py             flow kavos-lake-medallion: acquire → bak-extract → bronze-load
├── serve.py            sirve el flow con schedule cron (ACQUIRE_POLL_HOURS)
├── kavos_acquire/      adquisición Gmail → S3 presignado → mssql-backup/
│   ├── mailbox.py      poller IMAP (SINCE, remitente exacto, asunto, allowlist)
│   ├── resolvers.py    descarga por host (directo/S3, OneDrive, Drive)
│   ├── download.py     resume + checksum CRC32 + guarda de ventana y de disco
│   ├── identity.py     parseo del timestamp del nombre + guarda de antigüedad
│   ├── state.py        estado JSON atómico (Message-IDs, UIDs, último aplicado)
│   ├── retention.py    retención 1 (la misma que usa scripts/prune_backups.py)
│   └── pipeline.py     ensambla las piezas (acquire_latest_bak, mark_applied)
```

La única comunicación con el servicio es por abajo: el flow escribe Parquet y
carga Bronze, y la API/MCP lee el catálogo resultante. Nada de
`orchestration/` importa `src/kavos_lake/`, y nada de `src/` sabe que el flow
existe.

## Tests

```bash
.venv/bin/python -m pytest -q
```

`tests/conftest.py` agrega `src/` al `sys.path`, así no hace falta instalar el paquete.
