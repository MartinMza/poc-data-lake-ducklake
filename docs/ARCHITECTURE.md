# Arquitectura de Dulse

Organización **hexagonal (puertos y adaptadores)** con la **regla de dependencia**
de clean architecture como invariante verificable.

## Capas

```
src/dulse/
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

## Tests

```bash
.venv/bin/python -m pytest -q
```

`tests/conftest.py` agrega `src/` al `sys.path`, así no hace falta instalar el paquete.
