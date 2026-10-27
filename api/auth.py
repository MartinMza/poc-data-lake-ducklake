"""Autenticación de Dulse Lake: API keys (servicio) + JWT HS256 (usuarios).

Dos credenciales válidas, indistintas para cualquier ruta protegida:

- API key estática por header `X-API-Key` (integración máquina-a-máquina:
  dashboards, agentes, clientes MCP por HTTP).
- JWT `Authorization: Bearer <token>` emitido por `POST /auth/token` con
  usuario/password definidos en `AUTH_USERS` (personas, Swagger UI, scripts).

Configuración (env o `.env`):

| Var | Default | Notas |
|---|---|---|
| `AUTH_API_KEYS` | *(vacío)* | lista separada por comas; comparación constant-time |
| `AUTH_USERS` | *(vacío)* | `usuario:secreto,usuario2:secreto`; secreto plano o `pbkdf2_sha256$iter$salt$hash` |
| `AUTH_JWT_SECRET` | *(vacío)* | obligatorio si hay usuarios; secreto HS256 |
| `AUTH_JWT_TTL_MINUTES` | `60` | vigencia del access token |
| `AUTH_JWT_ISSUER` | `dulse-lake-api` | claim `iss` esperado |

Fail-closed: si no hay API keys ni usuarios configurados, la aplicación se
niega a arrancar (`assert_configured`). El transporte MCP por `stdio` corre
como proceso local del operador y no pasa por este módulo.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Iterable, Mapping

import jwt

from api.config import env, env_int

API_KEY_HEADER = "x-api-key"
API_KEYS_ENV = "AUTH_API_KEYS"
USERS_ENV = "AUTH_USERS"
JWT_SECRET_ENV = "AUTH_JWT_SECRET"
JWT_TTL_ENV = "AUTH_JWT_TTL_MINUTES"
JWT_ISSUER_ENV = "AUTH_JWT_ISSUER"

DEFAULT_TTL_MINUTES = 60
DEFAULT_ISSUER = "dulse-lake-api"
JWT_ALGORITHM = "HS256"

PBKDF2_PREFIX = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 260_000

# Piso de entropía para una API key usada como credencial de servicio. Un valor
# tipo "cambiame" pasaba el chequeo de presencia y habilitaba el servicio.
MIN_API_KEY_LENGTH = 24

# Techo defensivo de iteraciones PBKDF2: el valor viene del hash guardado en
# AUTH_USERS y se usa para derivar en cada login. Un typo (10**9) bloqueaba el
# worker minutos por intento.
MAX_PBKDF2_ITERATIONS = 1_000_000

# Rutas que no requieren credenciales: salud (healthcheck de Docker), raíz,
# documentación y el propio emisor de tokens.
PUBLIC_PATHS: tuple[str, ...] = (
    "/health",
    "/",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/openapi.json",
    "/auth/token",
)


class AuthError(Exception):
    """Credencial ausente, inválida o expirada (HTTP 401)."""

    status_code = 401

    def __init__(self, detail: str, headers: Mapping[str, str] | None = None):
        super().__init__(detail)
        self.detail = detail
        self.headers = dict(headers or {"WWW-Authenticate": "Bearer"})


class InvalidCredentials(AuthError):
    """Usuario/password incorrectos en `POST /auth/token`."""


def _split_list(raw: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (raw or "").split(",") if part.strip())


def api_keys() -> tuple[str, ...]:
    """API keys válidas configuradas en `AUTH_API_KEYS`."""
    return _split_list(env(API_KEYS_ENV))


def users() -> dict[str, str]:
    """Usuarios configurados en `AUTH_USERS` como `{usuario: secreto}`."""
    credentials: dict[str, str] = {}
    for entry in _split_list(env(USERS_ENV)):
        username, _, secret = entry.partition(":")
        if username.strip() and secret.strip():
            credentials[username.strip()] = secret.strip()
    return credentials


def jwt_secret() -> str:
    return (env(JWT_SECRET_ENV) or "").strip()


def jwt_issuer() -> str:
    return (env(JWT_ISSUER_ENV) or DEFAULT_ISSUER).strip() or DEFAULT_ISSUER


def jwt_ttl_minutes() -> int:
    ttl = env_int(JWT_TTL_ENV, DEFAULT_TTL_MINUTES)
    return ttl if ttl > 0 else DEFAULT_TTL_MINUTES


def assert_configured() -> None:
    """Fail-closed: sin credenciales utilizables no hay servicio."""
    keys = api_keys()
    configured_users = users()
    if not keys and not configured_users:
        raise RuntimeError(
            "Auth sin configurar: definí AUTH_API_KEYS (API keys) y/o AUTH_USERS "
            "(usuarios JWT) en el entorno o en .env."
        )
    # Una entrada mal formada se descartaba en silencio y el usuario "no existía".
    for entry in _split_list(env(USERS_ENV)):
        username, separator, secret = entry.partition(":")
        if not separator or not username.strip() or not secret.strip():
            raise RuntimeError(
                f"AUTH_USERS tiene una entrada inválida ({entry!r}): "
                "el formato es usuario:secreto."
            )
    for key in keys:
        if len(key) < MIN_API_KEY_LENGTH:
            raise RuntimeError(
                f"API key demasiado corta ({len(key)} caracteres): usá al menos "
                f"{MIN_API_KEY_LENGTH} (openssl rand -hex 24)."
            )
    if configured_users and not jwt_secret():
        raise RuntimeError(
            "AUTH_USERS está definido pero falta AUTH_JWT_SECRET para firmar los JWT."
        )


def hash_password(
    password: str,
    *,
    iterations: int = PBKDF2_ITERATIONS,
    salt: str | None = None,
) -> str:
    """Genera `pbkdf2_sha256$iteraciones$salt$hash` (apto para `AUTH_USERS`)."""
    salt = salt or secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return f"{PBKDF2_PREFIX}${iterations}${salt}${derived.hex()}"


def verify_password(stored: str, provided: str) -> bool:
    """Compara un secreto guardado (plano o PBKDF2) con el provisto."""
    if not stored or not provided:
        return False
    if not stored.startswith(f"{PBKDF2_PREFIX}$"):
        # comparar bytes: compare_digest rechaza str con caracteres no-ASCII
        return hmac.compare_digest(
            stored.encode("utf-8"), provided.encode("utf-8")
        )
    try:
        _, iterations_raw, salt, expected = stored.split("$", 3)
        iterations = int(iterations_raw)
    except ValueError:
        return False
    # pbkdf2_hmac lanza ValueError con iteraciones <= 0 (sería un 500) y un valor
    # enorme bloquea el worker: ambos son configuración inválida, no credencial.
    if not 0 < iterations <= MAX_PBKDF2_ITERATIONS:
        return False
    derived = hashlib.pbkdf2_hmac(
        "sha256", provided.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return hmac.compare_digest(derived.hex().encode("ascii"), expected.encode("ascii"))


def authenticate(username: str, password: str) -> str:
    """Valida usuario/password y devuelve el subject. Lanza InvalidCredentials."""
    stored = users().get(username)
    if stored is None or not verify_password(stored, password):
        # Mensaje único: no revelar si el usuario existe.
        raise InvalidCredentials("Usuario o password incorrectos.")
    return username


def create_access_token(
    subject: str,
    *,
    now: float | None = None,
    ttl_minutes: int | None = None,
) -> tuple[str, int]:
    """Emite un JWT HS256 para `subject`. Devuelve (token, segundos de vigencia)."""
    secret = jwt_secret()
    if not secret:
        raise RuntimeError(
            "Falta AUTH_JWT_SECRET: no se pueden emitir tokens JWT."
        )
    issued_at = int(now if now is not None else time.time())
    ttl = (ttl_minutes if ttl_minutes is not None else jwt_ttl_minutes()) * 60
    claims = {
        "sub": subject,
        "iss": jwt_issuer(),
        "iat": issued_at,
        "exp": issued_at + ttl,
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(claims, secret, algorithm=JWT_ALGORITHM), ttl


def decode_access_token(token: str) -> dict[str, Any]:
    """Valida firma, expiración e issuer. Devuelve los claims."""
    secret = jwt_secret()
    if not secret:
        raise AuthError("El servidor no tiene AUTH_JWT_SECRET configurado.")
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            issuer=jwt_issuer(),
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token expirado.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Token inválido.") from exc


def _matches_any_api_key(candidate: str, keys: Iterable[str]) -> bool:
    valid = False
    candidate_bytes = candidate.encode("utf-8")
    for key in keys:
        # Sin cortocircuito: se comparan todas las keys en tiempo constante.
        # En bytes: compare_digest rechaza str con caracteres no-ASCII.
        valid |= hmac.compare_digest(candidate_bytes, key.encode("utf-8"))
    return valid


def subject_from_headers(headers: Mapping[str, str]) -> str:
    """Resuelve el sujeto autenticado a partir de los headers de la request."""
    api_key = headers.get(API_KEY_HEADER)
    if api_key and _matches_any_api_key(api_key, api_keys()):
        return f"apikey:{api_key[:6]}…"

    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError(
            "Credenciales requeridas: header 'X-API-Key' o 'Authorization: Bearer <jwt>'."
        )
    claims = decode_access_token(token.strip())
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise AuthError("Token inválido: falta el claim 'sub'.")
    return f"jwt:{subject}"


class ApiAuthMiddleware:
    """Middleware ASGI que exige credenciales en toda ruta no pública.

    Cubre tanto las rutas REST de FastAPI como el sub-app MCP montado en
    `/mcp` (SSE/mensajes), porque el middleware envuelve la aplicación
    completa. Los preflight CORS (`OPTIONS`) pasan sin credenciales.
    """

    def __init__(
        self,
        app: Any,
        public_paths: Iterable[str] = PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self.public_paths = tuple(public_paths)

    def _is_public(self, path: str) -> bool:
        if path in self.public_paths:
            return True
        return any(path.startswith(f"{public}/") for public in self.public_paths)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope.get("method") == "OPTIONS" or self._is_public(path):
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        try:
            subject = subject_from_headers(headers)
        except AuthError as exc:
            await self._reject(send, exc)
            return

        state = scope.get("state")
        if isinstance(state, dict):
            state["auth"] = subject
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Any, exc: AuthError) -> None:
        body = json.dumps({"detail": exc.detail}).encode("utf-8")
        headers = [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in exc.headers.items()
        ]
        headers.append((b"content-type", b"application/json"))
        await send(
            {
                "type": "http.response.start",
                "status": exc.status_code,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})
