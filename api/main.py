"""Dulse Data Lake — FastAPI Application con endpoints REST y montaje FastMCP."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api import auth, auth_db, auth_panel, engine
from api.mcp_server import mcp

logger = logging.getLogger("dulse.auth")
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Arranque: valida el secreto JWT y prepara la base de credenciales."""
    auth.assert_configured()
    state = auth_db.bootstrap()
    if not state["available"]:
        logger.error("[auth] base de credenciales no disponible: %s", state.get("error"))
        logger.error("[auth] sin base no se autentica ninguna request de datos.")
    else:
        for note in state["notes"]:
            logger.warning("[auth] migración: %s", note)
        if state["setup_token"]:
            logger.warning("[auth] %s", "=" * 66)
            logger.warning("[auth] PRIMER ARRANQUE: todavía no hay superAdmin.")
            logger.warning("[auth] Abrí http://localhost:8000/auth/panel/configuracion y cargá este token:")
            logger.warning("[auth] SETUP TOKEN: %s", state["setup_token"])
            logger.warning(
                "[auth] vence en %s minutos y sirve una sola vez.",
                auth_db.SETUP_TOKEN_TTL_MINUTES,
            )
            logger.warning("[auth] %s", "=" * 66)
    yield


app = FastAPI(
    title="Dulse Data Lake API & MCP",
    version="1.0.0",
    # La documentación se sirve con guardas propias (ver más abajo): el esquema
    # de la API no queda expuesto sin sesión del panel.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
    description=(
        "API REST y servidor MCP para consultar la data cargada desde SQL Server "
        "en el DuckLake (esquema bronze, tablas mssql_*, con Time Travel). "
        "El panel de acceso está en /auth/panel."
    ),
)

# Auth envuelve TODO (rutas REST + sub-app MCP en /mcp). Se agrega antes que
# CORS para que CORS quede por fuera y los 401 también lleven sus headers.
app.add_middleware(auth.ApiAuthMiddleware, public_paths=auth.PUBLIC_PATHS)

# Habilitar CORS para integración con dashboards web / frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Montar servidor FastMCP con transporte SSE bajo /mcp
app.mount("/mcp", mcp.http_app(transport="sse"))

# Panel de acceso: HTML server-side (Jinja2) + fragmentos HTMX.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(auth_panel.router)


@app.exception_handler(auth_panel.SessionRequired)
def _needs_login(request: Request, exc: auth_panel.SessionRequired) -> RedirectResponse:
    return RedirectResponse(auth_panel.ENTRY_PATH, status_code=status.HTTP_303_SEE_OTHER)


@app.exception_handler(auth_panel.PasswordChangeRequired)
def _needs_password_change(request: Request, exc: auth_panel.PasswordChangeRequired) -> RedirectResponse:
    return RedirectResponse(auth_panel.PASSWORD_PATH, status_code=status.HTTP_303_SEE_OTHER)


@app.exception_handler(PermissionError)
def _forbidden(request: Request, exc: PermissionError) -> PlainTextResponse:
    return PlainTextResponse("Sólo el superAdmin puede hacer esto.", status_code=status.HTTP_403_FORBIDDEN)


@app.get("/ui", include_in_schema=False)
@app.get("/ui/", include_in_schema=False)
def legacy_ui_redirect() -> RedirectResponse:
    """La ruta vieja sobrevive como redirección permanente."""
    return RedirectResponse("/auth/panel", status_code=status.HTTP_308_PERMANENT_REDIRECT)


class TokenRequest(BaseModel):
    username: str = Field(..., min_length=1, description="Usuario definido en AUTH_USERS.")
    password: str = Field(..., min_length=1, description="Password (plano o PBKDF2 en AUTH_USERS).")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Vigencia del token en segundos.")


class QueryRequest(BaseModel):
    sql: str = Field(
        ...,
        description="Consulta SQL de solo lectura (SELECT, WITH, DESCRIBE, EXPLAIN).",
        examples=["SELECT * FROM lake.bronze.mssql_bs_transaccion LIMIT 5"],
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Cantidad máxima de filas a retornar (1 a 1000).",
    )


class QueryResponse(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    limit_applied: int
    sql_executed: str


@app.get("/", include_in_schema=False)
def root():
    """Redirige automáticamente a la documentación interactiva Swagger UI."""
    return RedirectResponse(url="/docs")


def _docs_gate(request: Request, path: str) -> RedirectResponse | None:
    """Sin sesión del panel no se ve la documentación: se manda a entrar."""
    if auth_panel.session_user(request) is not None:
        return None
    return RedirectResponse(
        f"{auth_panel.ENTRY_PATH}?next={path}", status_code=status.HTTP_303_SEE_OTHER
    )


@app.get("/openapi.json", include_in_schema=False)
def openapi_schema(request: Request):
    blocked = _docs_gate(request, "/openapi.json")
    if blocked is not None:
        return blocked
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
def swagger_ui(request: Request):
    blocked = _docs_gate(request, "/docs")
    if blocked is not None:
        return blocked
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Dulse Data Lake · API")


@app.get("/redoc", include_in_schema=False)
def redoc_ui(request: Request):
    blocked = _docs_gate(request, "/redoc")
    if blocked is not None:
        return blocked
    return get_redoc_html(openapi_url="/openapi.json", title="Dulse Data Lake · API")


@app.post("/auth/token", response_model=TokenResponse, tags=["Auth"])
def issue_token(payload: TokenRequest):
    """Emite un JWT (HS256) para un usuario de `AUTH_USERS`.

    Alternativa humana a la API key: el token sirve igual para la API REST y
    para el MCP por HTTP/SSE (`Authorization: Bearer <token>`).
    """
    try:
        subject = auth.authenticate(payload.username, payload.password)
    except auth.InvalidCredentials as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.detail,
            headers=exc.headers,
        ) from exc
    token, expires_in = auth.create_access_token(subject)
    return TokenResponse(access_token=token, expires_in=expires_in)


@app.get("/health", tags=["Health"])
def health_check():
    """Verifica el estado del servicio y conectividad básica."""
    return {
        "status": "healthy",
        "service": "dulse-lake-api",
        "version": "1.0.0",
        "mcp_mounted": True,
    }


def _custom_openapi():
    """OpenAPI con los dos esquemas de seguridad (botón Authorize de Swagger)."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update(
        {
            "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        }
    )
    schema["security"] = [{"ApiKeyAuth": []}, {"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


@app.get("/api/v1/tables", tags=["Catálogo"])
def list_lake_tables(
    schemas: str = Query(
        default="bronze",
        description="Esquemas separados por comas a incluir (el lago solo tiene bronze).",
    )
):
    """Lista las tablas del Data Lake con sus conteos de filas actuales."""
    schema_list = tuple(s.strip() for s in schemas.split(",") if s.strip())
    try:
        return engine.list_tables(schemas=schema_list)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error consultando tablas del lago: {exc}",
        ) from exc


@app.get("/api/v1/tables/{schema}/{table}", tags=["Catálogo"])
def describe_lake_table(schema: str, table: str):
    """Describe las columnas, tipos de datos y muestra de 5 filas de una tabla."""
    try:
        return engine.describe_table(schema=schema, table=table)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error describiendo tabla: {exc}",
        ) from exc


@app.get("/api/v1/sqlserver/tables", tags=["SQL Server"])
def list_sqlserver_tables():
    """Inventario de las tablas cargadas desde SQL Server.

    Devuelve por tabla: filas, cantidad de columnas, columnas y el último
    `_dlt_load_id` (marca de la corrida de ingesta más reciente).
    """
    try:
        return engine.sqlserver_tables()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error consultando las tablas de SQL Server: {exc}",
        ) from exc


@app.get("/api/v1/sqlserver/tables/{table}/rows", tags=["SQL Server"])
def preview_sqlserver_rows(
    table: str,
    limit: int = Query(default=20, ge=1, le=100, description="Filas por página (1-100)."),
    offset: int = Query(default=0, ge=0, description="Desplazamiento para paginar."),
    schema: str = Query(default="bronze", description="Esquema del lago."),
):
    """Devuelve una página de filas reales de una tabla cargada desde SQL Server."""
    try:
        return engine.preview_rows(schema=schema, table=table, limit=limit, offset=offset)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error leyendo la tabla '{table}': {exc}",
        ) from exc


@app.get("/api/v1/snapshots", tags=["Time Travel"])
def list_snapshots(
    limit: int = Query(
        default=10, ge=1, le=100, description="Cantidad de snapshots a listar."
    )
):
    """Lista los snapshots históricos del DuckLake para auditoría y Time Travel."""
    try:
        return engine.list_snapshots(limit=limit)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error obteniendo snapshots de DuckLake: {exc}",
        ) from exc


@app.post(
    "/api/v1/query",
    response_model=QueryResponse,
    tags=["SQL Sandbox"],
)
def run_sql_query(payload: QueryRequest):
    """Ejecuta una consulta SQL analítica en modo solo lectura con guardrails."""
    try:
        res = engine.execute_query(sql=payload.sql, limit=payload.limit)
        return res
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error de ejecución SQL: {exc}",
        ) from exc
