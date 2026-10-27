"""Endpoints del panel de credenciales (`/ui/api/*`).

Estos endpoints no usan la API key: se autentican con la cookie de sesión que
emiten `setup` y `login`. El middleware de API key exime todo `/ui`, así que la
protección de cada ruta vive acá y es explícita: router público para lo que debe
poder verse antes de entrar, y router privado con dependencia de sesión para
todo lo demás.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from api import auth_db

COOKIE_NAME = "dulse_session"

public_router = APIRouter(prefix="/ui/api", tags=["Panel"])
private_router = APIRouter(
    prefix="/ui/api", tags=["Panel"], dependencies=[Depends(lambda: None)]
)


class SetupRequest(BaseModel):
    token: str = Field(description="Token de setup impreso en el log del arranque")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    username: str
    password: str


class NewUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)
    role: str = "user"


class NewKeyRequest(BaseModel):
    label: str = ""
    expires_in_days: int = auth_db.MAX_EXPIRY_DAYS


def _set_session(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=auth_db.SESSION_TTL_MINUTES * 60,
        path="/",
    )


def current_user(request: Request) -> dict[str, Any]:
    """Sesión válida o 401. Es la única puerta de las rutas privadas."""
    token = request.cookies.get(COOKIE_NAME)
    with auth_db.connection() as conn:
        user = auth_db.session_user(conn, token or "")
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sesión ausente o vencida: iniciá sesión de nuevo.",
        )
    return user


def require_superadmin(
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> dict[str, Any]:
    if user["role"] != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta acción es sólo del superAdmin.",
        )
    return user


@public_router.get("/state")
def api_state(request: Request) -> dict[str, Any]:
    """Lo primero que consulta el front: ¿hay que configurar, o hay que entrar?"""
    token = request.cookies.get(COOKIE_NAME)
    with auth_db.connection() as conn:
        user = auth_db.session_user(conn, token or "")
        return {
            "needs_setup": not auth_db.has_superadmin(conn),
            "authenticated": user is not None,
            "user": user,
            "max_keys": auth_db.MAX_ACTIVE_KEYS,
            "max_expiry_days": auth_db.MAX_EXPIRY_DAYS,
        }


@public_router.post("/setup", status_code=status.HTTP_201_CREATED)
def api_setup(payload: SetupRequest, response: Response) -> dict[str, Any]:
    """Crea el superAdmin la primera vez. Exige el token de setup de un solo uso."""
    with auth_db.connection() as conn:
        if auth_db.has_superadmin(conn):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ya existe un superAdmin: este endpoint está cerrado.",
            )
        try:
            auth_db.consume_setup_token(conn, payload.token)
            user = auth_db.create_user(
                conn,
                username=payload.username,
                password=payload.password,
                role="superadmin",
            )
        except auth_db.AuthDbError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        token = auth_db.create_session(conn, user["id"])
    _set_session(response, token)
    return {"username": user["username"], "role": user["role"]}


@public_router.post("/login")
def api_login(payload: LoginRequest, response: Response) -> dict[str, Any]:
    with auth_db.connection() as conn:
        user = auth_db.verify_login(conn, payload.username, payload.password)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Usuario o password incorrectos.",
            )
        token = auth_db.create_session(conn, user["id"])
    _set_session(response, token)
    return {"username": user["username"], "role": user["role"]}


@public_router.post("/logout")
def api_logout(request: Request, response: Response) -> dict[str, str]:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with auth_db.connection() as conn:
            auth_db.delete_session(conn, token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "ok"}


@private_router.get("/me")
def api_me(user: Annotated[dict[str, Any], Depends(current_user)]) -> dict[str, Any]:
    with auth_db.connection() as conn:
        return {
            "user": user,
            "keys": auth_db.list_api_keys(conn, user["id"]),
            "max_keys": auth_db.MAX_ACTIVE_KEYS,
            "max_expiry_days": auth_db.MAX_EXPIRY_DAYS,
        }


@private_router.get("/users")
def api_users(
    user: Annotated[dict[str, Any], Depends(require_superadmin)],
) -> list[dict[str, Any]]:
    with auth_db.connection() as conn:
        return auth_db.list_users(conn)


@private_router.post("/users", status_code=status.HTTP_201_CREATED)
def api_create_user(
    payload: NewUserRequest,
    user: Annotated[dict[str, Any], Depends(require_superadmin)],
) -> dict[str, Any]:
    with auth_db.connection() as conn:
        try:
            created = auth_db.create_user(
                conn,
                username=payload.username,
                password=payload.password,
                role=payload.role,
            )
        except auth_db.AuthDbError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"id": created["id"], "username": created["username"], "role": created["role"]}


@private_router.post("/users/{user_id}/disable")
def api_disable_user(
    user_id: str,
    user: Annotated[dict[str, Any], Depends(require_superadmin)],
) -> dict[str, str]:
    with auth_db.connection() as conn:
        try:
            auth_db.disable_user(conn, user_id)
        except auth_db.AuthDbError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"status": "ok"}


@private_router.get("/keys")
def api_keys(user: Annotated[dict[str, Any], Depends(current_user)]) -> list[dict[str, Any]]:
    with auth_db.connection() as conn:
        return auth_db.list_api_keys(conn, user["id"])


@private_router.post("/keys", status_code=status.HTTP_201_CREATED)
def api_create_key(
    payload: NewKeyRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> dict[str, Any]:
    with auth_db.connection() as conn:
        try:
            key, row = auth_db.create_api_key(
                conn,
                user_id=user["id"],
                expires_in_days=payload.expires_in_days,
                label=payload.label,
            )
        except auth_db.AuthDbError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"key": key, "id": row["id"], "expires_at": row["expires_at"]}


@private_router.post("/keys/{key_id}/rotate", status_code=status.HTTP_201_CREATED)
def api_rotate_key(
    key_id: str,
    payload: NewKeyRequest,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> dict[str, Any]:
    with auth_db.connection() as conn:
        try:
            key, row = auth_db.rotate_api_key(
                conn,
                user_id=user["id"],
                key_id=key_id,
                expires_in_days=payload.expires_in_days,
                label=payload.label,
            )
        except auth_db.AuthDbError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"key": key, "id": row["id"], "expires_at": row["expires_at"]}


@private_router.post("/keys/{key_id}/revoke")
def api_revoke_key(
    key_id: str,
    user: Annotated[dict[str, Any], Depends(current_user)],
) -> dict[str, str]:
    with auth_db.connection() as conn:
        try:
            auth_db.revoke_api_key(conn, key_id=key_id, user_id=user["id"])
        except auth_db.AuthDbError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"status": "ok"}
