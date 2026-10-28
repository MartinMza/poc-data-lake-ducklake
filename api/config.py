"""Configuración por entorno con fallback a un `.env` local (sin dependencias)."""

from __future__ import annotations

import ipaddress
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str]:
    """Lee `.env` del directorio de trabajo una sola vez (best-effort)."""
    values: dict[str, str] = {}
    if not os.path.exists(".env"):
        return values
    try:
        with open(".env", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError:
        return {}
    return values


def env(name: str, default: str | None = None) -> str | None:
    """Variable de entorno con fallback a `.env` y luego al default."""
    value = os.getenv(name)
    if value is not None:
        return value
    return _dotenv().get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Lista separada por comas; vacia o ausente cae al default."""
    raw = env(name)
    if raw is None or not raw.strip():
        return tuple(default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def env_int(name: str, default: int) -> int:
    raw = env(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Valores que aparecen como default de ejemplo en el repo. Contra un host
# público son una credencial adivinable; contra un host interno (local, Docker,
# red privada) son parte del PoC.
DEFAULT_INSECURE_SECRETS = frozenset(
    {"", "ducklake", "password", "changeme", "secret", "admin"}
)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


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
