"""Credenciales deterministas de los tests (compartidas por las fixtures)."""

from __future__ import annotations

SUPERADMIN_USERNAME = "root"
SUPERADMIN_PASSWORD = "root-password-123"
USERNAME = "ana"
PASSWORD = "ana-password-123"
# 48 hex, mismo formato que una key real generada por `auth_db`.
TEST_API_KEY = "dulse_" + "a1b2c3d4" * 6


def auth_headers() -> dict[str, str]:
    """Headers listos para pegarle a la API en los tests."""
    return {"X-API-Key": TEST_API_KEY}


def seed() -> None:
    """Deja la base de prueba en un estado conocido (superAdmin + usuario + key)."""
    from dulse.adapters.outbound import postgres_auth as auth_db

    with auth_db.connection() as conn:
        conn.execute(
            "TRUNCATE users, api_keys, sessions, setup_tokens, meta RESTART IDENTITY CASCADE"
        )
        conn.commit()
        auth_db.create_user(
            conn,
            username=SUPERADMIN_USERNAME,
            password=SUPERADMIN_PASSWORD,
            role="superadmin",
        )
        user = auth_db.create_user(conn, username=USERNAME, password=PASSWORD)
        conn.execute(
            """
            INSERT INTO api_keys (user_id, label, visible_prefix, key_sha256, expires_at)
            VALUES (%s, 'tests', %s, %s, now() + interval '30 days')
            """,
            (
                user["id"],
                auth_db.visible_prefix(TEST_API_KEY),
                auth_db.hash_api_key(TEST_API_KEY),
            ),
        )
        conn.commit()
