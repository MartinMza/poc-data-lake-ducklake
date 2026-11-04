"""Puertos de identidad: usuarios, sesiones, API keys y rate-limit.

El adaptador de salida (Postgres) los implementa. Declararlos permite cambiar de
store (otra base, un servicio) sin tocar la capa de entrada.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UserRepositoryPort(Protocol):
    def create_user(
        self, conn: Any, *, username: str, password: str, role: str = "user"
    ) -> dict[str, Any]: ...

    def get_user(self, conn: Any, username: str) -> dict[str, Any] | None: ...

    def list_users(self, conn: Any) -> list[dict[str, Any]]: ...

    def verify_login(
        self, conn: Any, username: str, password: str
    ) -> dict[str, Any] | None: ...

    def set_user_password(
        self, conn: Any, user_id: str, password: str, *, temporary: bool = False
    ) -> None: ...

    def disable_user(self, conn: Any, user_id: str) -> None: ...

    def validate_username(self, username: str) -> str: ...

    def validate_password(self, password: str, *, username: str = "") -> str: ...

    def invalid_usernames(self, conn: Any) -> list[str]: ...


@runtime_checkable
class SessionRepositoryPort(Protocol):
    def create_session(self, conn: Any, user_id: str) -> str: ...

    def session_user(self, conn: Any, token: str) -> dict[str, Any] | None: ...

    def delete_session(self, conn: Any, token: str) -> None: ...

    def revoke_user_sessions(self, conn: Any, user_id: str) -> int: ...


@runtime_checkable
class ApiKeyRepositoryPort(Protocol):
    def create_api_key(
        self, conn: Any, user_id: str, *, label: str, expires_in_days: int
    ) -> dict[str, Any]: ...

    def rotate_api_key(
        self, conn: Any, user_id: str, key_id: str
    ) -> dict[str, Any]: ...

    def revoke_api_key(self, conn: Any, user_id: str, key_id: str) -> None: ...

    def list_api_keys(self, conn: Any, user_id: str) -> list[dict[str, Any]]: ...

    def authenticate_api_key(self, conn: Any, key: str) -> dict[str, Any] | None: ...

    def count_active_keys(self, conn: Any, user_id: str) -> int: ...


@runtime_checkable
class ThrottlePort(Protocol):
    def locked_for(self, key: str) -> float: ...

    def register_failure(self, key: str) -> float: ...

    def register_success(self, key: str) -> None: ...


@runtime_checkable
class ClockPort(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...
