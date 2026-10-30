"""Configuración por entorno: campos tipados (pydantic-settings) + resolver genérico.

Dos niveles, a propósito:

- `Settings`: los campos donde el tipo importa (flags de seguridad y límites).
  Los valida pydantic (booleanos, enteros con rango, lista CSV) y falla cerrado
  ante un valor inválido en vez de caer silenciosamente a un default inseguro.
- `env()`: resolver genérico para el resto (hosts, rutas, secretos) usando el
  mismo parser de `.env` que pydantic-settings (python-dotenv), así hay un solo
  formato soportado.

`get_settings()` **no** cachea: los tests cambian el entorno con
`monkeypatch.setenv` y el arranque valida fail-closed. El costo de instanciar
`Settings` es despreciable frente a una request.
"""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from typing import Annotated, Any

from dotenv import dotenv_values
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Orígenes locales por defecto: el panel y la API en localhost.
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)

# Valores que aparecen como default de ejemplo en el repo. Contra un host
# público son una credencial adivinable; contra un host interno (local, Docker,
# red privada) son parte del PoC.
DEFAULT_INSECURE_SECRETS = frozenset(
    {"", "ducklake", "password", "changeme", "secret", "admin"}
)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


def _split_csv(value: Any) -> list[str]:
    """Normaliza una lista separada por comas (acepta str, list o tuple)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value)]


class Settings(BaseSettings):
    """Configuración tipada de las perillas sensibles."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # NoDecode: el valor llega como CSV crudo y lo parte _parse_origins en vez
    # de que pydantic-settings intente json.loads sobre "a,b".
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default=list(DEFAULT_CORS_ORIGINS)
    )
    cors_allow_credentials: bool = False
    session_cookie_secure: bool = False
    auth_login_max_attempts: int = Field(default=5, ge=1)
    auth_login_window_seconds: int = Field(default=300, ge=1)
    auth_login_lockout_seconds: int = Field(default=300, ge=1)
    auth_jwt_ttl_minutes: int = Field(default=60, ge=1)

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: Any) -> list[str]:
        return _split_csv(value)


def get_settings() -> Settings:
    """Configuración tipada, leída del entorno y de `.env` en cada llamada."""
    return Settings()


# Nombres de entorno -> campo tipado del modelo.
_BOOL_FIELDS = {
    "CORS_ALLOW_CREDENTIALS": "cors_allow_credentials",
    "SESSION_COOKIE_SECURE": "session_cookie_secure",
}
_INT_FIELDS = {
    "AUTH_LOGIN_MAX_ATTEMPTS": "auth_login_max_attempts",
    "AUTH_LOGIN_WINDOW_SECONDS": "auth_login_window_seconds",
    "AUTH_LOGIN_LOCKOUT_SECONDS": "auth_login_lockout_seconds",
    "AUTH_JWT_TTL_MINUTES": "auth_jwt_ttl_minutes",
}
_LIST_FIELDS = {"CORS_ALLOW_ORIGINS": "cors_allow_origins"}


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str]:
    """Lee `.env` del directorio de trabajo una sola vez (best-effort)."""
    try:
        values = dotenv_values(".env")
    except OSError:
        return {}
    return {key: value for key, value in values.items() if value is not None}


def env(name: str, default: str | None = None) -> str | None:
    """Variable de entorno con fallback a `.env` y luego al default."""
    value = os.getenv(name)
    if value is not None:
        return value
    return _dotenv().get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    """Booleano: ausente cae al default del llamador; presente lo valida pydantic."""
    raw = env(name)
    if raw is None or not raw.strip():
        return default
    field = _BOOL_FIELDS.get(name)
    if field is not None:
        return bool(getattr(get_settings(), field))
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Lista separada por comas; vacía o ausente cae al default del llamador."""
    raw = env(name)
    if raw is None or not raw.strip():
        return tuple(default)
    field = _LIST_FIELDS.get(name)
    if field is not None:
        return tuple(getattr(get_settings(), field))
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def env_int(name: str, default: int) -> int:
    """Entero: ausente cae al default del llamador; presente lo valida pydantic."""
    raw = env(name)
    if raw is None or not raw.strip():
        return default
    field = _INT_FIELDS.get(name)
    if field is not None:
        return int(getattr(get_settings(), field))
    try:
        return int(raw)
    except ValueError:
        return default


def is_internal_host(host: str | None) -> bool:
    """True si el host es local, de red privada o un nombre de servicio Docker.

    Un nombre de un solo label (`postgres-catalog`, `garage`) es interno por
    definición; una IP privada/loopback/link-local también. Cualquier otra cosa
    (dominio o IP pública) se considera expuesta.
    """
    value = (host or "").strip().lower()
    if not value or value in LOOPBACK_HOSTS:
        return True
    if value.endswith(".local") or value.endswith(".internal"):
        return True
    if "." not in value:  # nombre de servicio Docker / hostname corto
        return True
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False  # dominio con punto: asumir público
    return ip.is_private or ip.is_loopback or ip.is_link_local


def assert_safe_credentials(*, host: str | None, secret: str | None, label: str) -> None:
    """Falla si una credencial de ejemplo apunta a un host no interno.

    No rompe local/Docker (ahí el default es aceptable), pero evita desplegar
    con `ducklake`/`password` contra un host público.
    """
    if is_internal_host(host):
        return
    if (secret or "").strip().lower() in DEFAULT_INSECURE_SECRETS:
        raise RuntimeError(
            f"{label}: credencial por defecto o vacía contra el host no interno "
            f"{host!r}. Definí una password explícita antes de exponer el servicio."
        )
