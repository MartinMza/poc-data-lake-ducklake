"""Tests de la persistencia de credenciales (`api/auth_db.py`).

Corren contra un Postgres real, en una base aparte (`dulse_auth_test`) que se
crea y se vacía sola: estas reglas no se pueden probar contra un mock sin
mentir sobre lo que hacen.
"""

from __future__ import annotations

import os

import psycopg
import pytest

os.environ.setdefault("AUTH_DB_NAME", "dulse_auth_test")

from dulse.adapters.outbound import postgres_auth as auth_db  # noqa: E402

TEST_DB = os.environ["AUTH_DB_NAME"]


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
    yield


@pytest.fixture()
def conn():
    with auth_db.connection() as connection:
        auth_db.ensure_schema(connection)
        connection.execute(
            "TRUNCATE users, api_keys, sessions, setup_tokens RESTART IDENTITY CASCADE"
        )
        connection.commit()
        yield connection


def _user(conn, username="ana", password="secreto-1234"):
    return auth_db.create_user(conn, username=username, password=password)


def test_password_is_stored_hashed(conn):
    _user(conn, password="mi-password-plano")
    row = auth_db.get_user(conn, "ana")
    assert row["password_hash"].startswith("pbkdf2_sha256$")
    assert "mi-password-plano" not in row["password_hash"]


def test_login_returns_user_only_with_the_right_password(conn):
    _user(conn)
    assert auth_db.verify_login(conn, "ana", "secreto-1234")["username"] == "ana"
    assert auth_db.verify_login(conn, "ana", "otra-cosa") is None
    assert auth_db.verify_login(conn, "nadie", "secreto-1234") is None


def test_duplicate_username_is_rejected(conn):
    _user(conn)
    with pytest.raises(auth_db.AuthDbError, match="ya existe"):
        _user(conn)


def test_disabled_user_cannot_log_in(conn):
    user = _user(conn)
    auth_db.disable_user(conn, user["id"])
    assert auth_db.verify_login(conn, "ana", "secreto-1234") is None


def test_api_key_is_returned_once_and_stored_hashed(conn):
    user = _user(conn)
    key, row = auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=30)
    assert key.startswith("dulse_")
    assert row["visible_prefix"] == key[:12]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM api_keys")
        stored = cur.fetchone()
    assert stored["key_sha256"] == auth_db.hash_api_key(key)
    assert key not in str(stored)
    listed = auth_db.list_api_keys(conn, user["id"])
    assert len(listed) == 1 and listed[0]["active"] is True


def test_api_key_authenticates_only_while_valid(conn):
    user = _user(conn)
    key, _ = auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=30)
    assert auth_db.authenticate_api_key(conn, key)["username"] == "ana"
    assert auth_db.authenticate_api_key(conn, "dulse_inventada") is None
    assert auth_db.authenticate_api_key(conn, "") is None


def test_expired_key_stops_working(conn):
    user = _user(conn)
    key, row = auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=1)
    conn.execute("UPDATE api_keys SET expires_at = now() - interval '1 hour' WHERE id = %s", (row["id"],))
    conn.commit()
    assert auth_db.authenticate_api_key(conn, key) is None


def test_fifth_key_is_allowed_and_sixth_is_rejected(conn):
    user = _user(conn)
    for _ in range(auth_db.MAX_ACTIVE_KEYS):
        auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=30)
    with pytest.raises(auth_db.AuthDbError, match="máximo 5"):
        auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=30)


def test_revoking_frees_a_slot(conn):
    user = _user(conn)
    keys = [
        auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=30)
        for _ in range(auth_db.MAX_ACTIVE_KEYS)
    ]
    auth_db.revoke_api_key(conn, key_id=keys[0][1]["id"], user_id=user["id"])
    auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=30)
    assert auth_db.count_active_keys(conn, user["id"]) == auth_db.MAX_ACTIVE_KEYS


def test_expiry_is_bounded_between_one_and_180_days(conn):
    user = _user(conn)
    with pytest.raises(auth_db.AuthDbError, match="entre 1 y 180"):
        auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=181)
    with pytest.raises(auth_db.AuthDbError, match="entre 1 y 180"):
        auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=0)
    _, row = auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=180)
    assert row["expires_at"] is not None


def test_rotate_works_even_with_all_slots_used(conn):
    user = _user(conn)
    keys = [
        auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=30)
        for _ in range(auth_db.MAX_ACTIVE_KEYS)
    ]
    old_key, old_row = keys[0]
    new_key, _ = auth_db.rotate_api_key(
        conn, user_id=user["id"], key_id=old_row["id"], expires_in_days=60
    )
    assert auth_db.authenticate_api_key(conn, old_key) is None
    assert auth_db.authenticate_api_key(conn, new_key)["username"] == "ana"
    assert auth_db.count_active_keys(conn, user["id"]) == auth_db.MAX_ACTIVE_KEYS


def test_a_user_cannot_revoke_someone_elses_key(conn):
    ana = _user(conn, username="ana")
    beto = _user(conn, username="beto", password="secreto-4567")
    _, row = auth_db.create_api_key(conn, user_id=beto["id"], expires_in_days=30)
    with pytest.raises(auth_db.AuthDbError, match="no es tuya"):
        auth_db.revoke_api_key(conn, key_id=row["id"], user_id=ana["id"])


def test_setup_token_is_single_use(conn):
    assert auth_db.has_superadmin(conn) is False
    token = auth_db.issue_setup_token(conn)
    auth_db.consume_setup_token(conn, token)
    with pytest.raises(auth_db.AuthDbError, match="ya usado"):
        auth_db.consume_setup_token(conn, token)
    with pytest.raises(auth_db.AuthDbError, match="inválido"):
        auth_db.consume_setup_token(conn, "token-inventado")


def test_session_roundtrip(conn):
    user = _user(conn)
    token = auth_db.create_session(conn, user["id"])
    assert auth_db.session_user(conn, token)["username"] == "ana"
    auth_db.delete_session(conn, token)
    assert auth_db.session_user(conn, token) is None


def test_has_superadmin_reflects_the_role(conn):
    assert auth_db.has_superadmin(conn) is False
    auth_db.create_user(conn, username="root", password="otro-secreto", role="superadmin")
    assert auth_db.has_superadmin(conn) is True


def test_key_migrada_sin_prefijo_tambien_autentica(conn):
    """Las keys importadas del `.env` no tienen el prefijo nuevo: deben seguir
    funcionando, o la migración dejaría a todos afuera."""
    user = _user(conn)
    legacy = "0123456789abcdef0123456789abcdef0123456789abcdef"
    conn.execute(
        """
        INSERT INTO api_keys (user_id, label, visible_prefix, key_sha256, expires_at)
        VALUES (%s, 'legacy', %s, %s, now() + interval '30 days')
        """,
        (user["id"], auth_db.visible_prefix(legacy), auth_db.hash_api_key(legacy)),
    )
    conn.commit()
    assert auth_db.authenticate_api_key(conn, legacy)["username"] == "ana"


# --- passwords, reset y activación (S2, S3, S4) ------------------------------


def test_password_corta_o_igual_al_usuario_se_rechaza(conn):
    with pytest.raises(auth_db.AuthDbError, match="al menos 12"):
        auth_db.validate_password("corta")
    with pytest.raises(auth_db.AuthDbError, match="nombre de usuario"):
        auth_db.validate_password("ana-ana-ana-ana", username="ana-ana-ana-ana")
    assert auth_db.validate_password("password-larga-1", username="ana") == "password-larga-1"


def test_el_alta_rechaza_una_password_debil():
    with auth_db.connection() as conn:
        with pytest.raises(auth_db.AuthDbError, match="al menos 12"):
            auth_db.create_user(conn, username="debil", password="corta")
        assert auth_db.get_user(conn, "debil") is None  # no quedó nada escrito


def test_password_aleatoria_es_legible_y_distinta_cada_vez():
    a, b = auth_db.generate_random_password(), auth_db.generate_random_password()
    assert a != b
    assert len(a) >= auth_db.MIN_PASSWORD_LENGTH
    assert a.count("-") == auth_db.RANDOM_PASSWORD_GROUPS - 1
    assert all(ch in auth_db.RANDOM_PASSWORD_ALPHABET or ch == "-" for ch in a)


def test_un_password_temporal_obliga_a_cambiarla_al_entrar(conn):
    usuario = auth_db.create_user(
        conn, username="temp", password="temporal-1234", must_change_password=True
    )
    assert usuario["must_change_password"] is True
    login = auth_db.verify_login(conn, "temp", "temporal-1234")
    assert login["must_change_password"] is True


def test_el_reset_corta_sesiones_del_panel_pero_no_las_api_keys(conn):
    usuario = auth_db.create_user(conn, username="beto2", password="secreto-1234")
    session = auth_db.create_session(conn, usuario["id"])
    key, _ = auth_db.create_api_key(conn, user_id=usuario["id"], expires_in_days=30)
    assert auth_db.session_user(conn, session) is not None

    auth_db.set_user_password(conn, usuario["id"], "nueva-password-1")

    assert auth_db.session_user(conn, session) is None, "la sesión del panel debe morir"
    assert auth_db.authenticate_api_key(conn, key) is not None, "la API key sigue viva"
    assert auth_db.verify_login(conn, "beto2", "nueva-password-1") is not None
    assert auth_db.verify_login(conn, "beto2", "secreto-1234") is None


def test_reset_con_password_temporal_deja_el_flag_prendido(conn):
    usuario = auth_db.create_user(conn, username="carla", password="secreto-1234")
    auth_db.set_user_password(conn, usuario["id"], "temporal-9999", must_change=True)
    assert auth_db.verify_login(conn, "carla", "temporal-9999")["must_change_password"] is True


def test_link_de_activacion_sirve_una_sola_vez(conn):
    usuario = auth_db.create_user(
        conn, username="dani", password="provisional-12", must_change_password=True
    )
    token = auth_db.issue_activation_token(conn, usuario["id"])

    assert auth_db.activation_token_user(conn, token)["username"] == "dani"
    auth_db.consume_activation_token(conn, token, "elegida-por-dani")

    assert auth_db.verify_login(conn, "dani", "elegida-por-dani") is not None
    assert auth_db.verify_login(conn, "dani", "provisional-1") is None
    # el flag quedó limpio: la password la eligió el usuario
    assert auth_db.verify_login(conn, "dani", "elegida-por-dani")["must_change_password"] is False
    with pytest.raises(auth_db.AuthDbError, match="ya se usó"):
        auth_db.consume_activation_token(conn, token, "otra-password-1")


def test_emitir_un_link_nuevo_invalida_el_anterior(conn):
    usuario = auth_db.create_user(conn, username="eve", password="provisional-12")
    viejo = auth_db.issue_activation_token(conn, usuario["id"])
    nuevo = auth_db.issue_activation_token(conn, usuario["id"])
    assert auth_db.activation_token_user(conn, viejo) is None
    assert auth_db.activation_token_user(conn, nuevo) is not None


def test_un_usuario_deshabilitado_no_puede_usar_el_link(conn):
    usuario = auth_db.create_user(conn, username="fede", password="provisional-12")
    token = auth_db.issue_activation_token(conn, usuario["id"])
    auth_db.disable_user(conn, usuario["id"])
    assert auth_db.activation_token_user(conn, token) is None
    with pytest.raises(auth_db.AuthDbError):
        auth_db.consume_activation_token(conn, token, "password-nueva-1")


# --- validación de nombres de usuario (S11) ----------------------------------


def test_un_nombre_con_espacios_se_rechaza_y_no_deja_rastro(conn):
    with pytest.raises(auth_db.AuthDbError, match="sin espacios"):
        auth_db.create_user(conn, username="Pepe Luis", password="secreto-1234")
    assert auth_db.get_user(conn, "Pepe Luis") is None


def test_nombres_validos_e_invalidos():
    for bueno in ("pepe.luis", "pepe_luis", "pepe-luis", "Pepe2", "ana"):
        assert auth_db.validate_username(bueno) == bueno
    for malo, motivo in (
        ("", "vacío"),
        ("ab", "3 y 64"),
        ("a" * 65, "3 y 64"),
        ("pepe luis", "sin espacios"),
        ("pepe@luis", "sin espacios"),
        ("pepé", "sin espacios"),
    ):
        with pytest.raises(auth_db.AuthDbError, match=motivo):
            auth_db.validate_username(malo)


def test_los_nombres_invalidos_que_ya_existen_se_detectan(conn):
    """La validación no toca lo que ya está: sólo lo reporta."""
    conn.execute("DELETE FROM users WHERE username = 'Viejo Usuario'")
    conn.execute(
        """
        INSERT INTO users (username, password_hash, role) VALUES ('Viejo Usuario', 'x', 'user')
        """
    )
    conn.commit()
    assert "Viejo Usuario" in auth_db.invalid_usernames(conn)
    conn.execute("DELETE FROM users WHERE username = 'Viejo Usuario'")
    conn.commit()
