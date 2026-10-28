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
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from api import auth, config

MAX_ACTIVE_KEYS = 5
MAX_EXPIRY_DAYS = 180
MIN_EXPIRY_DAYS = 1
SESSION_TTL_MINUTES = 480
SETUP_TOKEN_TTL_MINUTES = 30
ACTIVATION_TOKEN_TTL_HOURS = 24
MIN_PASSWORD_LENGTH = 12
RANDOM_PASSWORD_GROUPS = 4
RANDOM_PASSWORD_GROUP_LENGTH = 4
# Sin caracteres que se confunden al dictar: 0/O, 1/l/I, 5/S
RANDOM_PASSWORD_ALPHABET = "abcdefghjkmnpqrtuvwxyz2346789"

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
    must_change_password boolean NOT NULL DEFAULT false,
    password_changed_at timestamptz,
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

CREATE TABLE IF NOT EXISTS meta (
    key text PRIMARY KEY,
    value text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS setup_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_sha256 text NOT NULL UNIQUE,
    purpose text NOT NULL DEFAULT 'setup',
    user_id uuid REFERENCES users(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz
);
"""

# Columnas agregadas después de la primera versión del esquema: se aplican con
# ALTER ... IF NOT EXISTS para que una base ya creada se actualice sola.
SCHEMA_MIGRATIONS_SQL = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at timestamptz;
ALTER TABLE setup_tokens ADD COLUMN IF NOT EXISTS purpose text NOT NULL DEFAULT 'setup';
ALTER TABLE setup_tokens ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES users(id) ON DELETE CASCADE;
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
    config.assert_safe_credentials(
        host=host, secret=password, label="Base de credenciales"
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
        cur.execute(SCHEMA_MIGRATIONS_SQL)
    conn.commit()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_api_key(key: str) -> str:
    return _sha256_hex(key)


def visible_prefix(key: str) -> str:
    return key[:VISIBLE_PREFIX_LENGTH]


def now() -> datetime:
    return datetime.now(timezone.utc)


# --- nombres de usuario -----------------------------------------------------

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
USERNAME_RULE = (
    "El usuario debe tener entre 3 y 64 caracteres: letras, números, punto, "
    "guion y guion bajo (sin espacios ni acentos)."
)


def validate_username(username: str) -> str:
    """Reglas de nombre de usuario. Devuelve el nombre si es aceptable."""
    value = (username or "").strip()
    if not value:
        raise AuthDbError("El usuario no puede estar vacío.")
    if not USERNAME_PATTERN.fullmatch(value):
        raise AuthDbError(USERNAME_RULE)
    return value


def invalid_usernames(conn: psycopg.Connection) -> list[str]:
    """Nombres que ya están en la base y no cumplen la regla actual.

    No se renombran solos: un cambio de identidad es del humano. El arranque los
    reporta para que se decida qué hacer.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT username FROM users ORDER BY username")
        return [row["username"] for row in cur.fetchall() if not USERNAME_PATTERN.fullmatch(row["username"])]


# --- passwords --------------------------------------------------------------


def validate_password(password: str, *, username: str = "") -> str:
    """Reglas mínimas de password. Devuelve el password si es aceptable."""
    value = password or ""
    if len(value) < MIN_PASSWORD_LENGTH:
        raise AuthDbError(
            f"El password debe tener al menos {MIN_PASSWORD_LENGTH} caracteres "
            f"(tiene {len(value)})."
        )
    if username and value.strip().lower() == username.strip().lower():
        raise AuthDbError("El password no puede ser igual al nombre de usuario.")
    return value


def generate_random_password() -> str:
    """Password aleatoria legible: 4 grupos de 4 caracteres sin ambigüedad."""
    groups = [
        "".join(secrets.choice(RANDOM_PASSWORD_ALPHABET) for _ in range(RANDOM_PASSWORD_GROUP_LENGTH))
        for _ in range(RANDOM_PASSWORD_GROUPS)
    ]
    return "-".join(groups)


# --- usuarios ---------------------------------------------------------------


def create_user(
    conn: psycopg.Connection,
    *,
    username: str,
    password: str,
    role: str = "user",
    must_change_password: bool = False,
) -> dict[str, Any]:
    """Crea un usuario con el password hasheado (PBKDF2-SHA256 + salt).

    Con `must_change_password=True` la credencial es de un solo uso: el usuario
    entra y el panel lo obliga a definir una nueva antes de tocar nada más.
    """
    username = validate_username(username)
    if not password:
        raise AuthDbError("El password no puede estar vacío.")
    validate_password(password, username=username)
    if role not in ("superadmin", "user", "service"):
        raise AuthDbError(f"Rol inválido: {role}.")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (username, password_hash, role, must_change_password, password_changed_at)
            VALUES (%s, %s, %s, %s, CASE WHEN %s THEN NULL ELSE now() END)
            ON CONFLICT (username) DO NOTHING
            RETURNING id, username, role, created_at, must_change_password
            """,
            (
                username,
                auth.hash_password(password),
                role,
                bool(must_change_password),
                bool(must_change_password),
            ),
        )
        row = cur.fetchone()
    if row is None:
        raise AuthDbError(f"El usuario {username!r} ya existe.")
    conn.commit()
    return row


def get_user(conn: psycopg.Connection, username: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, username, password_hash, role, disabled_at, must_change_password
              FROM users WHERE username = %s
            """,
            (username,),
        )
        return cur.fetchone()


def list_users(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.role, u.created_at, u.disabled_at,
                   u.must_change_password, u.password_changed_at,
                   EXISTS (SELECT 1 FROM setup_tokens t
                            WHERE t.user_id = u.id AND t.purpose = 'activation'
                              AND t.consumed_at IS NULL AND t.expires_at > now()) AS pending_activation,
                   (SELECT count(*) FROM api_keys k
                     WHERE k.user_id = u.id AND k.revoked_at IS NULL
                       AND k.expires_at > now()) AS active_keys
            FROM users u
            WHERE u.role <> 'service'
            ORDER BY u.created_at
            """
        )
        return cur.fetchall()


# Hash señuelo: cuando el usuario no existe, está deshabilitado o es de
# servicio, se verifica igual contra este hash para que el tiempo de respuesta
# no delate qué usuarios existen (enumeración por timing).
_DUMMY_PASSWORD_HASH = auth.hash_password(secrets.token_urlsafe(16))


def verify_login(conn: psycopg.Connection, username: str, password: str) -> dict[str, Any] | None:
    """Devuelve la fila del usuario si las credenciales son válidas."""
    row = get_user(conn, (username or "").strip())
    if row is None or row["disabled_at"] is not None or row["role"] == "service":
        # Quemar el mismo tiempo que un login real: verificar contra el señuelo
        # y recién después negar. No cortar antes, para no filtrar por timing.
        auth.verify_password(_DUMMY_PASSWORD_HASH, password or "")
        return None
    if not auth.verify_password(row["password_hash"], password or ""):
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "must_change_password": bool(row.get("must_change_password")),
    }


def disable_user(conn: psycopg.Connection, user_id: str) -> None:
    """Deshabilita a un usuario común. Un superAdmin no se deshabilita desde acá."""
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row is None:
            raise AuthDbError("No existe el usuario.")
        if row["role"] == "superadmin":
            raise AuthDbError("Un superAdmin no se puede deshabilitar desde el panel.")
        cur.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (user_id,))
    conn.commit()


# --- reseteo de password y activación ---------------------------------------


def revoke_user_sessions(conn: psycopg.Connection, user_id: str) -> int:
    """Borra las sesiones del panel del usuario. Devuelve cuántas cerró."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        removed = cur.rowcount
    conn.commit()
    return int(removed)


def set_user_password(
    conn: psycopg.Connection,
    user_id: str,
    password: str,
    *,
    must_change: bool = False,
) -> dict[str, Any]:
    """Cambia el password y cierra las sesiones del panel de ese usuario.

    Decisión del usuario (S2): el reset corta sólo las sesiones del panel; las
    API keys y los JWT ya emitidos siguen vivos.
    """
    validated = validate_password(password, username=_username_of(conn, user_id))
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE users
               SET password_hash = %s,
                   must_change_password = %s,
                   password_changed_at = now()
             WHERE id = %s
            RETURNING id, username, role
            """,
            (auth.hash_password(validated), bool(must_change), user_id),
        )
        row = cur.fetchone()
        if row is None:
            raise AuthDbError("No existe el usuario.")
    conn.commit()
    revoke_user_sessions(conn, user_id)
    row["must_change_password"] = bool(must_change)
    return row


def _username_of(conn: psycopg.Connection, user_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
    return row["username"] if row else ""


def issue_activation_token(conn: psycopg.Connection, user_id: str) -> str:
    """Emite un link de activación de un solo uso (invalida los previos)."""
    token = secrets.token_urlsafe(32)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM setup_tokens WHERE user_id = %s AND purpose = 'activation'",
            (user_id,),
        )
        cur.execute(
            """
            INSERT INTO setup_tokens (token_sha256, purpose, user_id, expires_at)
            VALUES (%s, 'activation', %s, now() + %s * interval '1 hour')
            """,
            (_sha256_hex(token), user_id, ACTIVATION_TOKEN_TTL_HOURS),
        )
    conn.commit()
    return token


def activation_token_user(conn: psycopg.Connection, token: str) -> dict[str, Any] | None:
    """Devuelve el usuario del link si el token sirve; None si no aplica."""
    if not token:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.username, u.role, u.disabled_at
              FROM setup_tokens t JOIN users u ON u.id = t.user_id
             WHERE t.token_sha256 = %s AND t.purpose = 'activation'
               AND t.consumed_at IS NULL AND t.expires_at > now()
            """,
            (_sha256_hex(token),),
        )
        row = cur.fetchone()
    if row is None or row["disabled_at"] is not None:
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def consume_activation_token(
    conn: psycopg.Connection, token: str, new_password: str
) -> dict[str, Any]:
    """Define el password del usuario del link y quema el token."""
    user = activation_token_user(conn, token)
    if user is None:
        raise AuthDbError("El link de activación no sirve: venció, ya se usó o no existe.")
    validate_password(new_password, username=user["username"])
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE setup_tokens SET consumed_at = now()
             WHERE token_sha256 = %s AND consumed_at IS NULL AND expires_at > now()
            """,
            (_sha256_hex(token),),
        )
        if cur.rowcount == 0:
            raise AuthDbError("El link de activación no sirve: venció o ya se usó.")
        cur.execute(
            """
            UPDATE users
               SET password_hash = %s,
                   must_change_password = false,
                   password_changed_at = now()
             WHERE id = %s
            """,
            (auth.hash_password(new_password), user["id"]),
        )
    conn.commit()
    revoke_user_sessions(conn, user["id"])
    return user


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
    # Sin filtro de formato: las keys migradas del `.env` no tienen el prefijo
    # nuevo y tienen que seguir funcionando. Una key basura es un lookup que no
    # encuentra nada, nada más.
    if not key:
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
            SELECT u.id, u.username, u.role, u.must_change_password
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
        cur.execute("DELETE FROM setup_tokens WHERE purpose = 'setup' AND consumed_at IS NULL")
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


# --- arranque ---------------------------------------------------------------

LEGACY_MIGRATED_KEY = "legacy_env_migrated"
SERVICE_USERNAME = "service"
LEGACY_KEY_DAYS = 180


def migrate_legacy_env(conn: psycopg.Connection) -> list[str]:
    """Importa `AUTH_USERS`/`AUTH_API_KEYS` del entorno a la base, una sola vez.

    Existe para no dejar a nadie afuera al cambiar la fuente de verdad: las
    credenciales que hoy viven en `.env` pasan a la base y se siguen usando. Una
    vez migradas, el entorno deja de leerse.
    """
    notes: list[str] = []
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM meta WHERE key = %s", (LEGACY_MIGRATED_KEY,))
        if cur.fetchone() is not None:
            return notes

    legacy_users = auth.users()
    legacy_keys = auth.api_keys()
    if not legacy_users and not legacy_keys:
        return notes

    for raw_username, secret in legacy_users.items():
        try:
            username = validate_username(raw_username)
        except AuthDbError as exc:
            notes.append(f"usuario {raw_username!r} del entorno omitido: {exc}")
            continue
        password_hash = (
            secret if secret.startswith(f"{PBKDF2_PREFIX}$") else auth.hash_password(secret)
        )
        role = "user" if has_superadmin(conn) else "superadmin"
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (username, password_hash, role)
                VALUES (%s, %s, %s) ON CONFLICT (username) DO NOTHING
                """,
                (username, password_hash, role),
            )
        notes.append(f"usuario {username!r} importado del entorno (rol {role})")

    if legacy_keys:
        service_id = _ensure_service_user(conn)
        for index, key in enumerate(legacy_keys, start=1):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO api_keys (user_id, label, visible_prefix, key_sha256, expires_at)
                    VALUES (%s, %s, %s, %s, now() + %s * interval '1 day')
                    ON CONFLICT (key_sha256) DO NOTHING
                    """,
                    (
                        service_id,
                        f"migrada del entorno #{index}",
                        visible_prefix(key),
                        hash_api_key(key),
                        LEGACY_KEY_DAYS,
                    ),
                )
        notes.append(
            f"{len(legacy_keys)} API key(s) del entorno importadas al usuario "
            f"{SERVICE_USERNAME!r} con {LEGACY_KEY_DAYS} días de vigencia"
        )

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO meta (key, value) VALUES (%s, now()::text) ON CONFLICT (key) DO NOTHING",
            (LEGACY_MIGRATED_KEY,),
        )
    conn.commit()
    return notes


def _ensure_service_user(conn: psycopg.Connection) -> str:
    """Usuario sintético dueño de las keys migradas: no puede loguearse."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s", (SERVICE_USERNAME,))
        row = cur.fetchone()
        if row is not None:
            return row["id"]
        cur.execute(
            """
            INSERT INTO users (username, password_hash, role)
            VALUES (%s, %s, 'service')
            ON CONFLICT (username) DO UPDATE SET role = 'service'
            RETURNING id
            """,
            (SERVICE_USERNAME, auth.hash_password(secrets.token_urlsafe(32))),
        )
        return cur.fetchone()["id"]


def ensure_database() -> None:
    """Crea la base si no existe (idempotente)."""
    try:
        with psycopg.connect(dsn(), connect_timeout=3):
            return
    except psycopg.OperationalError as exc:
        if "does not exist" not in str(exc):
            raise
    admin_dsn = dsn().replace(f"dbname={database_name()}", "dbname=postgres")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{database_name()}"')


def bootstrap() -> dict[str, Any]:
    """Prepara la base al arrancar. Devuelve el estado para el log."""
    try:
        ensure_database()
        with connection() as conn:
            ensure_schema(conn)
            purge_expired(conn)
            notes = migrate_legacy_env(conn)
            needs_setup = not has_superadmin(conn)
            setup_token = issue_setup_token(conn) if needs_setup else None
            invalid = invalid_usernames(conn)
        return {
            "available": True,
            "needs_setup": needs_setup,
            "setup_token": setup_token,
            "notes": notes,
            "invalid_usernames": invalid,
        }
    except psycopg.Error as exc:
        return {
            "available": False,
            "error": str(exc),
            "needs_setup": False,
            "setup_token": None,
            "notes": [],
            "invalid_usernames": [],
        }
