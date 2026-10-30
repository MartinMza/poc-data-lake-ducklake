"""Rate-limiting de login en memoria (anti fuerza bruta de contraseñas).

Limita los intentos fallidos por usuario dentro de una ventana deslizante y, al
superar el umbral, bloquea ese usuario por un tiempo. Es por-proceso: alcanza
para el PoC de un solo worker. Con varios workers hay que mover el estado a un
store compartido (ej. Redis); se deja anotado a propósito.

La clave es el nombre de usuario normalizado (minúsculas, sin espacios): existan
o no, se tratan igual, para no filtrar por el comportamiento del lockout qué
usuarios existen.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from kavos_lake.infrastructure import config


def _normalize(key: str) -> str:
    return (key or "").strip().lower()


class LoginThrottle:
    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: float = 300.0,
        lockout_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def locked_for(self, key: str) -> float:
        """Segundos que falta para poder reintentar; 0 si no está bloqueado."""
        key = _normalize(key)
        now = self._clock()
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            remaining = until - now
            if remaining <= 0:
                self._locked_until.pop(key, None)
                return 0.0
            return remaining

    def register_failure(self, key: str) -> float:
        """Registra un intento fallido. Devuelve el lockout vigente en segundos."""
        key = _normalize(key)
        now = self._clock()
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            if until - now > 0:
                return until - now  # ya bloqueado: no apilar
            recent = [t for t in self._failures.get(key, []) if now - t < self.window_seconds]
            recent.append(now)
            if len(recent) >= self.max_attempts:
                self._locked_until[key] = now + self.lockout_seconds
                self._failures.pop(key, None)
                return self.lockout_seconds
            self._failures[key] = recent
            return 0.0

    def register_success(self, key: str) -> None:
        """Login exitoso: limpia el historial de ese usuario."""
        key = _normalize(key)
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


def _build_default() -> LoginThrottle:
    return LoginThrottle(
        max_attempts=config.env_int("AUTH_LOGIN_MAX_ATTEMPTS", 5),
        window_seconds=config.env_int("AUTH_LOGIN_WINDOW_SECONDS", 300),
        lockout_seconds=config.env_int("AUTH_LOGIN_LOCKOUT_SECONDS", 300),
    )


# Instancia compartida por el endpoint REST (/auth/token) y el panel.
login_throttle = _build_default()
