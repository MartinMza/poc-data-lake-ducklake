"""Configuración por entorno con fallback a un `.env` local (sin dependencias)."""

from __future__ import annotations

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


def env_int(name: str, default: int) -> int:
    raw = env(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default
