"""Panel de acceso server-side (`/auth/panel`).

Reemplaza la SPA de `/ui`: el HTML se arma en el servidor con Jinja2 y los
fragmentos se refrescan con HTMX. La autenticación del panel es la cookie de
sesión; la del API sigue siendo la API key o el JWT.

Reglas que vive acá y no en el template: quién puede ver qué (superAdmin),
qué hacer ante una contraseña temporal y cómo se emite una credencial de un
solo uso.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from api import auth, auth_db

COOKIE_NAME = "dulse_session"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
router = APIRouter(prefix="/auth/panel", tags=["Panel"], include_in_schema=False)

ENTRY_PATH = "/auth/panel/entrar"
PANEL_PATH = "/auth/panel"
PASSWORD_PATH = "/auth/panel/password"
SETUP_PATH = "/auth/panel/configuracion"


class SessionRequired(Exception):
    """No hay sesión: el handler redirige al login."""


class PasswordChangeRequired(Exception):
    """Hay sesión pero con contraseña temporal: primero hay que cambiarla."""


def _is_password_route(request: Request) -> bool:
    return urlsplit(request.url.path).path.rstrip("/") == PASSWORD_PATH


def _session_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(COOKIE_NAME)
    with auth_db.connection() as conn:
        return auth_db.session_user(conn, token or "")


def require_session(request: Request) -> dict[str, Any]:
    """Sesión válida y contraseña definitiva, o excepción que redirige."""
    user = _session_user(request)
    if user is None:
        raise SessionRequired()
    if user.get("must_change_password") and not _is_password_route(request):
        raise PasswordChangeRequired()
    return user


def _set_session(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=auth_db.SESSION_TTL_MINUTES * 60,
        path="/",
    )


def _clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _render(
    request: Request,
    template: str,
    *,
    user: dict[str, Any] | None = None,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    payload = {
        "request": request,
        "user": user,
        "min_password": auth_db.MIN_PASSWORD_LENGTH,
        "max_keys": auth_db.MAX_ACTIVE_KEYS,
        "max_expiry_days": auth_db.MAX_EXPIRY_DAYS,
        "min_expiry_days": auth_db.MIN_EXPIRY_DAYS,
        "default_days": auth_db.MAX_EXPIRY_DAYS,
        "activation_hours": auth_db.ACTIVATION_TOKEN_TTL_HOURS,
        "ttl_minutes": auth_db.SETUP_TOKEN_TTL_MINUTES,
    }
    payload.update(context)
    return templates.TemplateResponse(request, template, payload, status_code=status_code)


def _redirect(path: str, *, ok: str | None = None, code: int = status.HTTP_303_SEE_OTHER) -> RedirectResponse:
    if ok:
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}ok={ok}"
    return RedirectResponse(path, status_code=code)


def _base_url(request: Request) -> str:
    configured = (os.environ.get("PANEL_PUBLIC_URL") or "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def activation_link(request: Request, token: str) -> str:
    return f"{_base_url(request)}/auth/panel/activar/{token}"


# --- entrar / salir ----------------------------------------------------------


@router.get("/entrar", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    if _session_user(request) is not None:
        return _redirect(PANEL_PATH)
    ok = request.query_params.get("ok")
    done = "Contraseña actualizada. Entrá con la nueva." if ok == "password" else None
    return _render(request, "login.html", error=None, done=done, username="")


@router.post("/entrar")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
) -> Response:
    with auth_db.connection() as conn:
        user = auth_db.verify_login(conn, username, password)
        if user is None:
            return _render(
                request,
                "login.html",
                error="Usuario o contraseña incorrectos.",
                done=None,
                username=username,
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        token = auth_db.create_session(conn, user["id"])
    target = PASSWORD_PATH if user.get("must_change_password") else PANEL_PATH
    response = _redirect(target)
    _set_session(response, token)
    return response


@router.post("/salir")
def logout(request: Request) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with auth_db.connection() as conn:
            auth_db.delete_session(conn, token)
    response = _redirect(ENTRY_PATH)
    _clear_session(response)
    return response


# --- primera configuración ---------------------------------------------------


@router.get("/configuracion", response_class=HTMLResponse)
def setup_page(request: Request) -> Response:
    with auth_db.connection() as conn:
        if auth_db.has_superadmin(conn):
            return _redirect(ENTRY_PATH)
    return _render(request, "setup.html", error=None, username="admin")


@router.post("/configuracion")
def setup_submit(
    request: Request,
    token: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
) -> Response:
    def fail(message: str, code: int = status.HTTP_400_BAD_REQUEST) -> Response:
        return _render(request, "setup.html", error=message, username=username, status_code=code)

    with auth_db.connection() as conn:
        if auth_db.has_superadmin(conn):
            return _redirect(ENTRY_PATH)
        if password != password2:
            return fail("Las contraseñas no coinciden.")
        try:
            auth_db.consume_setup_token(conn, token)
            user = auth_db.create_user(conn, username=username, password=password, role="superadmin")
        except auth_db.AuthDbError as exc:
            return fail(str(exc))
        session = auth_db.create_session(conn, user["id"])
    response = _redirect(PANEL_PATH, ok="setup")
    _set_session(response, session)
    return response


# --- activación por link -----------------------------------------------------


@router.get("/activar/{token}", response_class=HTMLResponse)
def activate_page(request: Request, token: str) -> Response:
    with auth_db.connection() as conn:
        user = auth_db.activation_token_user(conn, token)
    if user is None:
        return _render(
            request,
            "activate.html",
            error="Este link no sirve: venció o ya se usó. Pedí uno nuevo al administrador.",
            username="",
            token=token,
            status_code=status.HTTP_410_GONE,
        )
    return _render(request, "activate.html", error=None, username=user["username"], token=token)


@router.post("/activar/{token}")
def activate_submit(
    request: Request,
    token: str,
    password: str = Form(""),
    password2: str = Form(""),
) -> Response:
    with auth_db.connection() as conn:
        user = auth_db.activation_token_user(conn, token)
        if user is None:
            return _render(
                request,
                "activate.html",
                error="Este link no sirve: venció o ya se usó.",
                username="",
                token=token,
                status_code=status.HTTP_410_GONE,
            )
        if password != password2:
            return _render(
                request,
                "activate.html",
                error="Las contraseñas no coinciden.",
                username=user["username"],
                token=token,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            auth_db.consume_activation_token(conn, token, password)
        except auth_db.AuthDbError as exc:
            return _render(
                request,
                "activate.html",
                error=str(exc),
                username=user["username"],
                token=token,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        session = auth_db.create_session(conn, user["id"])
    response = _redirect(PANEL_PATH, ok="activated")
    _set_session(response, session)
    return response


# --- mi contraseña -----------------------------------------------------------


@router.get("/password", response_class=HTMLResponse)
def password_page(request: Request) -> Response:
    user = _session_user(request)
    if user is None:
        return _redirect(ENTRY_PATH)
    return _render(
        request,
        "password.html",
        user=user,
        forced=bool(user.get("must_change_password")),
        error=None,
        tab="password",
    )


@router.post("/password")
def password_submit(
    request: Request,
    current: str = Form(""),
    password: str = Form(""),
    password2: str = Form(""),
) -> Response:
    user = _session_user(request)
    if user is None:
        return _redirect(ENTRY_PATH)

    def fail(message: str) -> Response:
        return _render(
            request,
            "password.html",
            user=user,
            forced=bool(user.get("must_change_password")),
            error=message,
            tab="password",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    with auth_db.connection() as conn:
        if auth_db.verify_login(conn, user["username"], current) is None:
            return fail("La contraseña actual no es correcta.")
        if password != password2:
            return fail("Las contraseñas nuevas no coinciden.")
        try:
            auth_db.set_user_password(conn, user["id"], password)
        except auth_db.AuthDbError as exc:
            return fail(str(exc))
        # El cambio cierra las sesiones; la propia se renueva para no expulsarte.
        session = auth_db.create_session(conn, user["id"])
    response = _redirect(PANEL_PATH, ok="password")
    _set_session(response, session)
    return response


# --- mis API keys ------------------------------------------------------------


def _keys_section(
    request: Request,
    user: dict[str, Any],
    *,
    created: str | None = None,
    error: str | None = None,
    done: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    with auth_db.connection() as conn:
        keys = auth_db.list_api_keys(conn, user["id"])
        active = auth_db.count_active_keys(conn, user["id"])
    return _render(
        request,
        "_keys_section.html",
        user=user,
        keys=keys,
        active_keys=active,
        created=created,
        error=error,
        done=done,
        tab="keys",
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def panel_home(request: Request) -> Response:
    user = require_session(request)
    with auth_db.connection() as conn:
        keys = auth_db.list_api_keys(conn, user["id"])
        active = auth_db.count_active_keys(conn, user["id"])
    done = {"setup": "Listo: superAdmin creado.", "activated": "Cuenta activada."}.get(
        request.query_params.get("ok", "")
    )
    return _render(
        request,
        "keys.html",
        user=user,
        keys=keys,
        active_keys=active,
        created=None,
        error=None,
        done=done,
        tab="keys",
    )


@router.post("/keys")
def create_key(
    request: Request,
    label: str = Form(""),
    expires_in_days: int = Form(auth_db.MAX_EXPIRY_DAYS),
) -> Response:
    user = require_session(request)
    with auth_db.connection() as conn:
        try:
            key, _row = auth_db.create_api_key(
                conn,
                user_id=user["id"],
                expires_in_days=expires_in_days,
                label=label.strip()[:80],
            )
        except auth_db.AuthDbError as exc:
            return _keys_section(request, user, error=str(exc), status_code=400)
    return _keys_section(request, user, created=key)


@router.post("/keys/{key_id}/rotar")
def rotate_key(request: Request, key_id: str) -> Response:
    user = require_session(request)
    with auth_db.connection() as conn:
        try:
            key, _row = auth_db.rotate_api_key(conn, key_id=key_id, user_id=user["id"])
        except auth_db.AuthDbError as exc:
            return _keys_section(request, user, error=str(exc), status_code=400)
    return _keys_section(request, user, created=key)


@router.post("/keys/{key_id}/revocar")
def revoke_key(request: Request, key_id: str) -> Response:
    user = require_session(request)
    with auth_db.connection() as conn:
        try:
            auth_db.revoke_api_key(conn, key_id=key_id, user_id=user["id"])
        except auth_db.AuthDbError as exc:
            return _keys_section(request, user, error=str(exc), status_code=400)
    return _keys_section(request, user, done="Key revocada.")


# --- usuarios (superAdmin) ---------------------------------------------------


def _require_superadmin(request: Request) -> dict[str, Any]:
    user = require_session(request)
    if user["role"] != "superadmin":
        raise PermissionError()
    return user


def _users_section(
    request: Request,
    user: dict[str, Any],
    *,
    created: dict[str, Any] | None = None,
    error: str | None = None,
    done: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    with auth_db.connection() as conn:
        users = auth_db.list_users(conn)
    return _render(
        request,
        "_users_section.html",
        user=user,
        users=users,
        created=created,
        error=error,
        done=done,
        tab="users",
        status_code=status_code,
    )


@router.get("/usuarios", response_class=HTMLResponse)
def users_page(request: Request) -> Response:
    user = require_session(request)
    if user["role"] != "superadmin":
        return _render(
            request,
            "forbidden.html",
            user=user,
            message="Esta sección es sólo del superAdmin.",
            status_code=status.HTTP_403_FORBIDDEN,
        )
    with auth_db.connection() as conn:
        users = auth_db.list_users(conn)
    return _render(
        request, "users.html", user=user, users=users, created=None, error=None, done=None, tab="users"
    )


def _credential_for(
    request: Request,
    conn: Any,
    user_id: str,
    username: str,
    mode: str,
) -> dict[str, Any]:
    """Emite la credencial de un solo uso: link de activación o password temporal."""
    if mode == "random":
        temporary = auth_db.generate_random_password()
        auth_db.set_user_password(conn, user_id, temporary, must_change=True)
        return {"kind": "password", "username": username, "value": temporary}
    token = auth_db.issue_activation_token(conn, user_id)
    return {"kind": "link", "username": username, "value": activation_link(request, token)}


@router.post("/usuarios")
def create_user(
    request: Request,
    username: str = Form(""),
    role: str = Form("user"),
    mode: str = Form("link"),
) -> Response:
    user = _require_superadmin(request)
    mode = "random" if mode == "random" else "link"
    with auth_db.connection() as conn:
        try:
            created_user = auth_db.create_user(
                conn,
                username=username,
                # Contraseña provisoria que nadie conoce: la real llega por el
                # link de activación o por la temporal que se muestra una vez.
                password=auth_db.generate_random_password(),
                role="superadmin" if role == "superadmin" else "user",
                must_change_password=True,
            )
            credential = _credential_for(
                request, conn, created_user["id"], created_user["username"], mode
            )
        except auth_db.AuthDbError as exc:
            return _users_section(request, user, error=str(exc), status_code=400)
    return _users_section(request, user, created=credential)


@router.post("/usuarios/{user_id}/reset")
def reset_password(request: Request, user_id: str, mode: str = Form("link")) -> Response:
    user = _require_superadmin(request)
    mode = "random" if mode == "random" else "link"
    with auth_db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
        if row is None:
            return _users_section(request, user, error="No existe el usuario.", status_code=404)
        username = row["username"]
        try:
            credential = _credential_for(request, conn, user_id, username, mode)
        except auth_db.AuthDbError as exc:
            return _users_section(request, user, error=str(exc), status_code=400)
    return _users_section(request, user, created=credential)


@router.post("/usuarios/{user_id}/deshabilitar")
def disable_user(request: Request, user_id: str) -> Response:
    user = _require_superadmin(request)
    with auth_db.connection() as conn:
        try:
            auth_db.disable_user(conn, user_id)
        except auth_db.AuthDbError as exc:
            return _users_section(request, user, error=str(exc), status_code=400)
    return _users_section(request, user, done="Usuario deshabilitado.")
