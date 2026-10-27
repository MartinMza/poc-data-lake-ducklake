"""Fixtures compartidas: base de credenciales de prueba y credenciales fijas.

Los tests corren contra un Postgres real, en una base aparte
(`dulse_auth_test`) que se crea y se siembra sola. Nada de mocks: lo que se
prueba es exactamente lo que va a correr en producción.
"""

from __future__ import annotations

import os

import psycopg
import pytest

TEST_DB = "dulse_auth_test"
# Sin credenciales legadas: la migración del entorno no debe correr en tests.
os.environ["AUTH_USERS"] = ""
os.environ["AUTH_API_KEYS"] = ""
os.environ["AUTH_DB_NAME"] = TEST_DB
os.environ.setdefault(
    "AUTH_JWT_SECRET", "test-jwt-secret-0123456789abcdef0123456789abcdef"
)

from api import auth, auth_db  # noqa: E402

from api import test_support  # noqa: E402
from api.test_support import (  # noqa: E402
    PASSWORD,
    SUPERADMIN_PASSWORD,
    SUPERADMIN_USERNAME,
    TEST_API_KEY,
    USERNAME,
)


def _dsn(dbname: str) -> str:
    return auth_db.dsn().replace(f"dbname={TEST_DB}", f"dbname={dbname}")


@pytest.fixture(scope="session", autouse=True)
def _database():
    with psycopg.connect(_dsn("postgres"), autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    with auth_db.connection() as conn:
        auth_db.ensure_schema(conn)
    test_support.seed()
    auth._API_KEY_CACHE.clear()
    yield
