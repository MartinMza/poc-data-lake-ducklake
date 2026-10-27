"""Persistencia de credenciales (usuarios, API keys y sesiones) en Postgres.

Vive en una base aparte, `dulse_auth`, dentro del mismo servidor que el catálogo
DuckLake: el esquema del lake no se toca y su backup sigue siendo sólo del
catálogo.

Reglas de negocio que este módulo garantiza (no la UI):

- el password se guarda hasheado con PBKDF2-SHA256 (nunca en claro);
- de cada API key se guarda sólo su SHA-256 y un prefijo visible;
- como máximo `MAX_ACTIVE_KEYS` keys activas por usuario;
- el vencimiento de una key va de 1 a `MAX_EXPIRY_DAYS` días.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from api import auth

MAX_ACTIVE_KEYS = 5
MAX_EXPIRY_DAYS = 180
MIN_EXPIRY_DAYS = 1
SESSION_TTL_MINUTES = 480
SETUP_TOKEN_TTL_MINUTES = 30

KEY_PREFIX = "dulse_"
KEY_RANDOM_BYTES = 24
VISIBLE_PREFIX_LENGTH = 12
PBKDF2_PREFIX = "pbkdf2_sha256"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'user',
    created_at timestamptz NOT NULL DEFAULT now(),
    disabled_at timestamptz,
    CONSTRAINT users_role_check CHECK (role IN ('superadmin', 'user', 'service'))
);

CREATE TABLE IF NOT EXISTS api_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label text NOT NULL DEFAULT '',
    visible_prefix text NOT NULL,
    key_sha256 text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    last_used_at timestamptz
);

CREATE INDEX IF NOT EXISTS api_keys_user_active_idx
    ON api_keys (user_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_sha256 text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS setup_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_sha256 text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz
);
"""


class AuthDbError(Exception):
    """Configuración inválida, regla de negocio violada o credencial inválida."""


def database_name() -> str:
    return (os.environ.get("AUTH_DB_NAME") or "dulse_auth").strip()


def dsn() -> str:
    """DSN de la base de auth: mismos host/puerto/usuario que el catálogo."""
    host = os.environ.get("AUTH_DB_HOST") or os.environ.get("CATALOG_HOST") or "localhost"
    port = os.environ.get("AUTH_DB_PORT") or os.environ.get("CATALOG_PORT") or "5433"
    user = os.environ.get("AUTH_DB_USER") or os.environ.get("CATALOG_USER") or "ducklake"
    password = (
        os.environ.get("AUTH_DB_PASSWORD")
        or os.environ.get("CATALOG_PASSWORD")
        or "ducklake"
    )
    return f"host={host} port={port} user={user} password={password} dbname={database_name()}"


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn(), row_factory=dict_row) as conn:
        yield conn


def ensure_schema(conn: psycopg.Connection) -> None:
    """Crea las tablas si faltan. Idempotente: se llama en cada arranque."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_api_key(key: str) -> str:
    return _sha256_hex(key)


def visible_prefix(key: str) -> str:
    return key[:VISIBLE_PREFIX_LENGTH]


def now() -> datetime:
    return datetime.now(timezone.utc)


# --- usuarios ---------------------------------------------------------------


def create_user(
    conn: psycopg.Connection,
    *,
    username: str,
    password: str,
    role: str = "user",
) -> dict[str, Any]:
    """Crea un usuario con el password hasheado (PBKDF2-SHA256 + salt)."""
    username = (username or "").strip()
    if not username:
        raise AuthDbError("El usuario no puede estar vacío.")
    if not password:
        raise AuthDbError("El password no puede estar vacío.")
    if role not in ("superadmin", "user", "service"):
        raise AuthDbError(f"Rol inválido: {role}.")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (username, password_hash, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (username) DO NOTHING
            RETURNING id, username, role, created_at
            """,
            (username, auth.hash_password(password), role),
        )
        row = cur.fetchone()
    if row is None:
        raise AuthDbError(f"El usuario {username!r} ya existe.")
    conn.commit()
    return row


def get_user(conn: psycopg.Connection, username: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, password_hash, role, disabled_at FROM users WHERE username = %s",
            (username,),
        )
        return cur.fetchone()


def list_users(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.role, u.created_at, u.disabled_at,
                   (SELECT count(*) FROM api_keys k
                     WHERE k.user_id = u.id AND k.revoked_at IS NULL
                       AND k.expires_at > now()) AS active_keys
            FROM users u
            WHERE u.role <> 'service'
            ORDER BY u.created_at
            """
        )
        return cur.fetchall()


def verify_login(conn: psycopg.Connection, username: str, password: str) -> dict[str, Any] | None:
    """Devuelve la fila del usuario si las credenciales son válidas."""
    row = get_user(conn, (username or "").strip())
    if row is None or row["disabled_at"] is not None:
        return None
    if row["role"] == "service":
        return None
    if not auth.verify_password(row["password_hash"], password or ""):
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def disable_user(conn: psycopg.Connection, user_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET disabled_at = now() WHERE id = %s AND role <> 'superadmin'",
            (user_id,),
        )
        if cur.rowcount == 0:
            raise AuthDbError("No existe el usuario, o es el superAdmin.")
    conn.commit()


# --- API keys ---------------------------------------------------------------


def count_active_keys(conn: psycopg.Connection, user_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS n FROM api_keys
             WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now()
            """,
            (user_id,),
        )
        return int(cur.fetchone()["n"])


def _validate_expiry_days(expires_in_days: int) -> int:
    days = int(expires_in_days)
    if days < MIN_EXPIRY_DAYS or days > MAX_EXPIRY_DAYS:
        raise AuthDbError(
            f"El vencimiento debe estar entre {MIN_EXPIRY_DAYS} y {MAX_EXPIRY_DAYS} días "
            f"(pediste {days})."
        )
    return days


def create_api_key(
    conn: psycopg.Connection,
    *,
    user_id: str,
    expires_in_days: int = 90,
    label: str = "",
) -> tuple[str, dict[str, Any]]:
    """Crea una key nueva. Devuelve (key en claro, fila) — la key no se repite."""
    days = _validate_expiry_days(expires_in_days)
    key = KEY_PREFIX + secrets.token_hex(KEY_RANDOM_BYTES)
    with conn.cursor() as cur:
        # Serializa las creaciones por usuario: el límite de 5 no se puede violar
        # por dos requests concurrentes.
        cur.execute("SELECT id FROM users WHERE id = %s FOR UPDATE", (user_id,))
        if cur.fetchone() is None:
            raise AuthDbError("No existe el usuario dueño de la key.")
        cur.execute(
            """
            SELECT count(*) AS n FROM api_keys
             WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now()
            """,
            (user_id,),
        )
        active = int(cur.fetchone()["n"])
        if active >= MAX_ACTIVE_KEYS:
            raise AuthDbError(
                f"Ya tenés {active} API keys activas (máximo {MAX_ACTIVE_KEYS}). "
                "Revocá una antes de crear otra."
            )
        cur.execute(
            """
            INSERT INTO api_keys (user_id, label, visible_prefix, key_sha256, expires_at)
            VALUES (%s, %s, %s, %s, now() + %s * interval '1 day')
            RETURNING id, label, visible_prefix, created_at, expires_at
            """,
            (user_id, label.strip(), visible_prefix(key), hash_api_key(key), days),
        )
        row = cur.fetchone()
    conn.commit()
    return key, row


def rotate_api_key(
    conn: psycopg.Connection,
    *,
    user_id: str,
    key_id: str,
    expires_in_days: int = 90,
    label: str = "",
) -> tuple[str, dict[str, Any]]:
    """Revoca una key y crea su reemplazo en la misma transacción."""
    revoke_api_key(conn, key_id=key_id, user_id=user_id, commit=False)
    return create_api_key(conn, user_id=user_id, expires_in_days=expires_in_days, label=label)


def list_api_keys(conn: psycopg.Connection, user_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, label, visible_prefix, created_at, expires_at, revoked_at, last_used_at,
                   (revoked_at IS NULL AND expires_at > now()) AS active
              FROM api_keys WHERE user_id = %s
             ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return cur.fetchall()


def revoke_api_key(
    conn: psycopg.Connection,
    *,
    key_id: str,
    user_id: str | None = None,
    commit: bool = True,
) -> None:
    with conn.cursor() as cur:
        if user_id is None:
            cur.execute(
                "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
                (key_id,),
            )
        else:
            cur.execute(
                """
                UPDATE api_keys SET revoked_at = now()
                 WHERE id = %s AND user_id = %s AND revoked_at IS NULL
                """,
                (key_id, user_id),
            )
        if cur.rowcount == 0:
            raise AuthDbError("No existe esa key, no es tuya, o ya estaba revocada.")
    if commit:
        conn.commit()


def authenticate_api_key(conn: psycopg.Connection, key: str) -> dict[str, Any] | None:
    """Devuelve el usuario dueño si la key existe, no venció y no fue revocada."""
    if not key or not key.startswith(KEY_PREFIX):
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT k.id AS key_id, u.id, u.username, u.role
              FROM api_keys k JOIN users u ON u.id = k.user_id
             WHERE k.key_sha256 = %s AND k.revoked_at IS NULL
               AND k.expires_at > now() AND u.disabled_at IS NULL
            """,
            (hash_api_key(key),),
        )
        row = cur.fetchone()
        if row is not None:
            cur.execute("UPDATE api_keys SET last_used_at = now() WHERE id = %s", (row["key_id"],))
            conn.commit()
    return row


# --- sesiones del panel ------------------------------------------------------


def create_session(conn: psycopg.Connection, user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sessions (user_id, token_sha256, expires_at)
            VALUES (%s, %s, now() + %s * interval '1 minute')
            """,
            (user_id, _sha256_hex(token), SESSION_TTL_MINUTES),
        )
    conn.commit()
    return token


def session_user(conn: psycopg.Connection, token: str) -> dict[str, Any] | None:
    if not token:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.role
              FROM sessions s JOIN users u ON u.id = s.user_id
             WHERE s.token_sha256 = %s AND s.expires_at > now() AND u.disabled_at IS NULL
            """,
            (_sha256_hex(token),),
        )
        return cur.fetchone()


def delete_session(conn: psycopg.Connection, token: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token_sha256 = %s", (_sha256_hex(token),))
    conn.commit()


# --- primer arranque ---------------------------------------------------------


def has_superadmin(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE role = 'superadmin' AND disabled_at IS NULL LIMIT 1")
        return cur.fetchone() is not None


def issue_setup_token(conn: psycopg.Connection) -> str:
    """Emite un token de setup de un solo uso (invalida los anteriores)."""
    token = secrets.token_urlsafe(24)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM setup_tokens WHERE consumed_at IS NULL")
        cur.execute(
            """
            INSERT INTO setup_tokens (token_sha256, expires_at)
            VALUES (%s, now() + %s * interval '1 minute')
            """,
            (_sha256_hex(token), SETUP_TOKEN_TTL_MINUTES),
        )
    conn.commit()
    return token


def consume_setup_token(conn: psycopg.Connection, token: str) -> None:
    """Quema el token. Falla si no existe, ya se usó o venció."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE setup_tokens SET consumed_at = now()
             WHERE token_sha256 = %s AND consumed_at IS NULL AND expires_at > now()
            """,
            (_sha256_hex(token or ""),),
        )
        if cur.rowcount == 0:
            raise AuthDbError("Token de setup inválido, vencido o ya usado.")
    conn.commit()


def purge_expired(conn: psycopg.Connection) -> None:
    """Limpieza barata de sesiones y tokens vencidos (se llama en el arranque)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE expires_at < now()")
        cur.execute("DELETE FROM setup_tokens WHERE expires_at < now() - interval '1 day'")
    conn.commit()
