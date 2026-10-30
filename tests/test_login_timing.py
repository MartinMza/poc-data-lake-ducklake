"""Login en tiempo constante (anti-enumeración de usuarios por timing).

Antes, un usuario inexistente volvía `None` sin computar hash, mientras que uno
existente pagaba PBKDF2 (260k iteraciones). Esa diferencia de tiempo delataba
qué usuarios existen. Ahora el camino de "no puede loguearse" verifica igual
contra un hash señuelo.
"""

from __future__ import annotations

from dulse.adapters.outbound import postgres_auth as auth_db
from dulse.infrastructure import security as auth
from tests.support import USERNAME


def test_usuario_inexistente_verifica_contra_senuelo(monkeypatch):
    calls: list[str] = []
    real = auth.verify_password
    monkeypatch.setattr(
        auth,
        "verify_password",
        lambda stored, provided: calls.append(stored) or real(stored, provided),
    )
    with auth_db.connection() as conn:
        assert auth_db.verify_login(conn, "usuario-que-no-existe", "x" * 12) is None
    assert calls, "debe verificar un hash aunque el usuario no exista"
    assert calls[0] == auth_db._DUMMY_PASSWORD_HASH


def test_hash_senuelo_es_pbkdf2_valido():
    assert auth_db._DUMMY_PASSWORD_HASH.startswith("pbkdf2_sha256$")


def test_login_valido_sigue_funcionando():
    from tests.support import PASSWORD

    with auth_db.connection() as conn:
        user = auth_db.verify_login(conn, USERNAME, PASSWORD)
    assert user is not None and user["username"] == USERNAME
