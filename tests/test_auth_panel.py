"""Tests del panel de acceso server-side (`/auth/panel`).

Se pega por HTTP como lo haría un navegador: formularios, redirects y cookies.
Nada de llamar funciones internas para el flujo de UI.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from kavos_lake.adapters.inbound.http import panel as auth_panel
from kavos_lake.adapters.outbound import postgres_auth as auth_db
from tests import support as test_support
from kavos_lake.adapters.inbound.http.api import app
from tests.support import (
    PASSWORD,
    SUPERADMIN_PASSWORD,
    SUPERADMIN_USERNAME,
    TEST_API_KEY,
    USERNAME,
)

PANEL = "/auth/panel"
KEY_RE = re.compile(r'<code id="nueva-key">([^<]+)</code>')
CRED_RE = re.compile(r'<code id="nueva-cred">([^<]+)</code>')


@pytest.fixture(autouse=True)
def _bypass_csrf(monkeypatch):
    """CSRF tiene su propia suite (test_csrf.py). Estos tests cubren la lógica
    del panel, no la validación del token, así que se neutraliza acá."""
    monkeypatch.setattr(auth_panel, "_require_csrf", lambda *args, **kwargs: None)


@pytest.fixture(scope="module", autouse=True)
def _known_state():
    """Re-siembra: otros módulos truncan la base de prueba entre medio."""
    test_support.seed()
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def browser():
    """Cliente con su propio tarro de cookies (simula otro navegador)."""
    with TestClient(app) as other:
        yield other


def _login(client, username=USERNAME, password=PASSWORD):
    return client.post(
        f"{PANEL}/entrar",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _login_admin(client):
    return _login(client, SUPERADMIN_USERNAME, SUPERADMIN_PASSWORD)


def _new_user(username: str, password: str = "provisional-1234") -> dict:
    with auth_db.connection() as conn:
        conn.execute("DELETE FROM users WHERE username = %s", (username,))
        conn.commit()
        return auth_db.create_user(conn, username=username, password=password)


def _all_routes(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _all_routes(inner.routes)
        elif hasattr(route, "path") and hasattr(route, "methods"):
            yield route


# --- entrar / salir ----------------------------------------------------------


def test_login_page_renders_a_form(client):
    response = client.get(f"{PANEL}/entrar")
    assert response.status_code == 200
    assert 'name="password"' in response.text
    assert "Entrar al panel" in response.text


def test_panel_without_session_redirects_to_login(client):
    response = client.get(PANEL, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"{PANEL}/entrar"


def test_wrong_password_shows_the_error_inline(client):
    response = _login(client, USERNAME, "password-incorrecta")
    assert response.status_code == 401
    assert "incorrectos" in response.text
    assert 'name="password"' in response.text, "el formulario sigue ahí para reintentar"


def test_login_sets_a_session_cookie_and_opens_the_panel(client):
    response = _login(client)
    assert response.status_code == 303
    assert response.headers["location"] == PANEL
    assert client.cookies.get("kavos_lake_session")
    page = client.get(PANEL)
    assert page.status_code == 200
    assert "Mis API keys" in page.text


def test_logout_kills_the_session(client):
    _login(client)
    assert client.get(PANEL).status_code == 200
    client.post(f"{PANEL}/salir", follow_redirects=False)
    assert client.get(PANEL, follow_redirects=False).status_code == 303


def test_legacy_ui_route_redirects_permanently(client):
    response = client.get("/ui", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == PANEL


# --- API keys desde el panel -------------------------------------------------


def test_panel_lists_the_users_existing_key(client):
    _login(client)
    page = client.get(PANEL)
    assert TEST_API_KEY[:12] in page.text, "el prefijo visible de la key sembrada"


def test_creating_a_key_shows_it_once_and_then_lists_it(client):
    _login(client)
    response = client.post(
        f"{PANEL}/keys", data={"label": "desde el panel", "expires_in_days": 30}
    )
    assert response.status_code == 200
    match = KEY_RE.search(response.text)
    assert match, "la key recién creada se muestra una sola vez"
    key = match.group(1)
    assert key.startswith("kavos_lake_")
    with auth_db.connection() as conn:
        assert auth_db.authenticate_api_key(conn, key) is not None
    refreshed = client.get(PANEL)
    assert "desde el panel" in refreshed.text
    assert key not in refreshed.text, "al recargar, la key en claro ya no está"


def test_key_limit_is_enforced_by_the_server():
    user = _new_user("limite-keys")
    with TestClient(app) as browser:
        assert _login(browser, "limite-keys", "provisional-1234").status_code == 303
        for _ in range(auth_db.MAX_ACTIVE_KEYS):
            assert browser.post(f"{PANEL}/keys", data={"expires_in_days": 10}).status_code == 200
        response = browser.post(f"{PANEL}/keys", data={"expires_in_days": 10})
        assert response.status_code == 400
        assert str(auth_db.MAX_ACTIVE_KEYS) in response.text
    with auth_db.connection() as conn:
        assert auth_db.count_active_keys(conn, user["id"]) == auth_db.MAX_ACTIVE_KEYS


def test_expiry_over_the_cap_is_rejected():
    _new_user("vencimiento-largo")
    with TestClient(app) as browser:
        _login(browser, "vencimiento-largo", "provisional-1234")
        response = browser.post(
            f"{PANEL}/keys", data={"expires_in_days": auth_db.MAX_EXPIRY_DAYS + 1}
        )
        assert response.status_code == 400
        assert str(auth_db.MAX_EXPIRY_DAYS) in response.text


def test_rotating_from_the_panel_invalidates_the_old_key():
    _new_user("rota-key")
    with TestClient(app) as browser:
        assert _login(browser, "rota-key", "provisional-1234").status_code == 303
        created = browser.post(f"{PANEL}/keys", data={"expires_in_days": 10})
        old_key = KEY_RE.search(created.text).group(1)
        concepts = browser.get(PANEL).text
        key_id = re.search(r"/keys/([0-9a-f-]{36})/rotar", concepts).group(1)

        rotated = browser.post(f"{PANEL}/keys/{key_id}/rotar")
        assert rotated.status_code == 200
        new_key = KEY_RE.search(rotated.text).group(1)
        assert new_key != old_key
        with auth_db.connection() as conn:
            assert auth_db.authenticate_api_key(conn, old_key) is None
            assert auth_db.authenticate_api_key(conn, new_key) is not None


def test_revoking_from_the_panel_kills_the_key():
    _new_user("revoca-key")
    with TestClient(app) as browser:
        assert _login(browser, "revoca-key", "provisional-1234").status_code == 303
        created = browser.post(f"{PANEL}/keys", data={"expires_in_days": 10})
        key = KEY_RE.search(created.text).group(1)
        key_id = re.search(r"/keys/([0-9a-f-]{36})/rotar", browser.get(PANEL).text).group(1)

        assert browser.post(f"{PANEL}/keys/{key_id}/revocar").status_code == 200
        with auth_db.connection() as conn:
            assert auth_db.authenticate_api_key(conn, key) is None


# --- contraseña propia -------------------------------------------------------


def test_wrong_current_password_keeps_the_user_out_of_the_change(client):
    _login(client)
    response = client.post(
        f"{PANEL}/password",
        data={"current": "no-es", "password": "nueva-password-1", "password2": "nueva-password-1"},
    )
    assert response.status_code == 400
    assert "no es correcta" in response.text


def test_changing_the_password_keeps_you_logged_in(client):
    _new_user("cambia-clave", "inicial-12345")
    with TestClient(app) as browser:
        assert _login(browser, "cambia-clave", "inicial-12345").status_code == 303
        response = browser.post(
            f"{PANEL}/password",
            data={
                "current": "inicial-12345",
                "password": "definitiva-123",
                "password2": "definitiva-123",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert browser.get(PANEL).status_code == 200, "la sesión propia se renueva"
    with auth_db.connection() as conn:
        assert auth_db.verify_login(conn, "cambia-clave", "definitiva-123") is not None
        assert auth_db.verify_login(conn, "cambia-clave", "inicial-12345") is None


def test_short_password_is_rejected_with_the_rule(client):
    _login(client)
    response = client.post(
        f"{PANEL}/password",
        data={"current": PASSWORD, "password": "corta", "password2": "corta"},
    )
    assert response.status_code == 400
    assert str(auth_db.MIN_PASSWORD_LENGTH) in response.text


# --- usuarios (superAdmin) ---------------------------------------------------


def test_a_regular_user_cannot_reach_the_users_section(client):
    _login(client)
    response = client.get(f"{PANEL}/usuarios")
    assert response.status_code == 403
    assert "superAdmin" in response.text


def test_superadmin_creates_a_user_with_an_activation_link(client):
    _login_admin(client)
    with auth_db.connection() as conn:
        conn.execute("DELETE FROM users WHERE username = 'nuevo-link'")
        conn.commit()
    response = client.post(
        f"{PANEL}/usuarios", data={"username": "nuevo-link", "role": "user", "mode": "link"}
    )
    assert response.status_code == 200
    link = CRED_RE.search(response.text).group(1)
    path = urlsplit(link).path
    assert path.startswith(f"{PANEL}/activar/")

    page = client.get(path)
    assert page.status_code == 200
    assert "nuevo-link" in page.text

    activated = client.post(
        path,
        data={"password": "elegida-por-mi-1", "password2": "elegida-por-mi-1"},
        follow_redirects=False,
    )
    assert activated.status_code == 303
    assert client.get(PANEL).status_code == 200, "queda con sesión iniciada"

    assert client.get(path).status_code == 410, "el link sirve una sola vez"
    with auth_db.connection() as conn:
        login = auth_db.verify_login(conn, "nuevo-link", "elegida-por-mi-1")
        assert login is not None and login["must_change_password"] is False


def test_temporary_password_forces_a_change_at_login(client):
    _login_admin(client)
    with auth_db.connection() as conn:
        conn.execute("DELETE FROM users WHERE username = 'nuevo-temp'")
        conn.commit()
    response = client.post(
        f"{PANEL}/usuarios", data={"username": "nuevo-temp", "role": "user", "mode": "random"}
    )
    assert response.status_code == 200
    temporary = CRED_RE.search(response.text).group(1)
    assert len(temporary) >= auth_db.MIN_PASSWORD_LENGTH

    with TestClient(app) as browser:
        login = _login(browser, "nuevo-temp", temporary)
        assert login.status_code == 303
        assert login.headers["location"] == f"{PANEL}/password", "primero cambia la clave"
        assert browser.get(PANEL, follow_redirects=False).headers["location"] == f"{PANEL}/password"
        changed = browser.post(
            f"{PANEL}/password",
            data={"current": temporary, "password": "mia-propia-123", "password2": "mia-propia-123"},
            follow_redirects=False,
        )
        assert changed.status_code == 303
        assert browser.get(PANEL).status_code == 200


def test_admin_reset_revokes_the_users_panel_sessions_only(client, browser):
    user = _new_user("reseteame", "inicial-12345")
    with auth_db.connection() as conn:
        key, _ = auth_db.create_api_key(conn, user_id=user["id"], expires_in_days=10)

    assert _login(browser, "reseteame", "inicial-12345").status_code == 303
    assert browser.get(PANEL).status_code == 200

    _login_admin(client)
    response = client.post(f"{PANEL}/usuarios/{user['id']}/reset", data={"mode": "random"})
    assert response.status_code == 200
    assert CRED_RE.search(response.text)

    assert browser.get(PANEL, follow_redirects=False).status_code == 303, "sesión del panel cortada"
    with auth_db.connection() as conn:
        assert auth_db.authenticate_api_key(conn, key) is not None, "la API key sigue viva"


def test_reset_with_a_link_invalidates_the_previous_link(client):
    user = _new_user("dos-links", "inicial-12345")
    _login_admin(client)
    first = CRED_RE.search(
        client.post(f"{PANEL}/usuarios/{user['id']}/reset", data={"mode": "link"}).text
    ).group(1)
    second = CRED_RE.search(
        client.post(f"{PANEL}/usuarios/{user['id']}/reset", data={"mode": "link"}).text
    ).group(1)
    assert first != second
    assert client.get(urlsplit(first).path).status_code == 410
    assert client.get(urlsplit(second).path).status_code == 200


def test_disabling_a_user_blocks_login_and_open_sessions(client, browser):
    user = _new_user("adios", "inicial-12345")
    assert _login(browser, "adios", "inicial-12345").status_code == 303
    _login_admin(client)
    response = client.post(f"{PANEL}/usuarios/{user['id']}/deshabilitar")
    assert response.status_code == 200
    assert "deshabilitado" in response.text
    assert browser.get(PANEL, follow_redirects=False).status_code == 303
    assert _login(browser, "adios", "inicial-12345").status_code == 401


def test_setup_page_is_closed_once_a_superadmin_exists(client):
    response = client.get(f"{PANEL}/configuracion", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"{PANEL}/entrar"


# --- convivencia de rutas (S8) ----------------------------------------------


def test_panel_does_not_swallow_token_api_or_mcp_routes(client):
    token = client.post("/auth/token", json={"username": USERNAME, "password": PASSWORD})
    assert token.status_code == 200, "POST /auth/token sigue siendo de la API"
    assert token.json()["access_token"]

    data = client.get("/api/v1/sqlserver/tables", headers={"X-API-Key": TEST_API_KEY})
    assert data.status_code == 200, "la API de datos no fue capturada por el panel"
    assert isinstance(data.json(), list)

    assert client.get("/health").status_code == 200


def test_no_duplicate_routes_and_no_panel_prefix_collisions():
    seen: dict[tuple[str, str], str] = {}
    for route in _all_routes(app.routes):
        for method in route.methods:
            key = (method, route.path)
            assert key not in seen, f"ruta duplicada: {method} {route.path}"
            seen[key] = route.path
    assert "/auth/panel" in {path for _method, path in seen}
    panel_paths = {path for _method, path in seen if path.startswith("/auth/panel")}
    assert panel_paths, "el panel está montado"
    for method, path in seen:
        if path.startswith("/auth/panel"):
            continue
        assert not path.startswith("/api/v1") or method in {"GET", "POST"}, path


def test_admin_cannot_disable_their_own_account(client):
    """Auto-deshabilitarse sería quedar afuera para siempre (feedback de diseño)."""
    _login_admin(client)
    with auth_db.connection() as conn:
        me = conn.execute(
            "SELECT id FROM users WHERE username = %s", (SUPERADMIN_USERNAME,)
        ).fetchone()
    response = client.post(f"{PANEL}/usuarios/{me['id']}/deshabilitar")
    assert response.status_code == 400
    assert "propia cuenta" in response.text
    with auth_db.connection() as conn:
        row = conn.execute(
            "SELECT disabled_at FROM users WHERE username = %s", (SUPERADMIN_USERNAME,)
        ).fetchone()
    assert row["disabled_at"] is None, "sigue activo"
    assert client.get(PANEL).status_code == 200, "y sigue adentro"


def test_my_own_row_offers_no_disable_button(client):
    _login_admin(client)
    with auth_db.connection() as conn:
        me = conn.execute(
            "SELECT id FROM users WHERE username = %s", (SUPERADMIN_USERNAME,)
        ).fetchone()
    page = client.get(f"{PANEL}/usuarios").text
    assert f"/usuarios/{me['id']}/deshabilitar" not in page
    assert "Mi contraseña" in page, "en su lugar, el camino a cambiarla"


# --- Swagger detrás del login (S9) -------------------------------------------


def test_docs_require_a_panel_session(browser):
    """Sin sesión (tarro de cookies limpio) la documentación no se sirve."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        response = browser.get(path, follow_redirects=False)
        assert response.status_code == 303, f"{path} no debería ser público"
        assert response.headers["location"] == f"{PANEL}/entrar?next={path}"


def test_docs_open_once_logged_in(client):
    _login(client)
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/v1/sqlserver/tables" in schema.json()["paths"]
    assert client.get("/redoc").status_code == 200


def test_login_returns_to_the_requested_page(client):
    response = client.post(
        f"{PANEL}/entrar",
        data={"username": USERNAME, "password": PASSWORD, "next": "/docs"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/docs"
    assert client.get("/docs").status_code == 200


def test_login_refuses_to_redirect_off_site(client):
    response = client.post(
        f"{PANEL}/entrar",
        data={"username": USERNAME, "password": PASSWORD, "next": "//evil.example/x"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == PANEL, "un next externo se ignora"


def test_temporary_password_wins_over_the_next_parameter(client):
    with auth_db.connection() as conn:
        conn.execute("DELETE FROM users WHERE username = 'next-temp'")
        conn.commit()
        auth_db.create_user(
            conn, username="next-temp", password="temporal-1234", must_change_password=True
        )
    response = client.post(
        f"{PANEL}/entrar",
        data={"username": "next-temp", "password": "temporal-1234", "next": "/docs"},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"{PANEL}/password", "primero cambia la clave"


# --- botón de ver contraseña (S10) -------------------------------------------


def test_password_fields_offer_a_reveal_button(client):
    esperado = {
        f"{PANEL}/entrar": ['"password"'],
        f"{PANEL}/activar/link-invalido": ['"password"', '"password2"'],
    }
    for path, ids in esperado.items():
        page = client.get(path).text
        for field_id in ids:
            assert f'data-reveal={field_id}' in page or f"data-reveal={field_id[1:-1]}" in page, (
                f"{path} no ofrece ver el campo {field_id}"
            )
    setuppage = client.get(f"{PANEL}/configuracion", follow_redirects=False)
    if setuppage.status_code == 200:
        assert 'data-reveal="password"' in setuppage.text


def test_change_password_form_offers_reveal_for_every_field(client):
    _login(client)
    page = client.get(f"{PANEL}/password").text
    for field_id in ("current", "password", "password2"):
        assert f'data-reveal="{field_id}"' in page


def test_the_panel_rejects_a_username_with_spaces(client):
    _login_admin(client)
    response = client.post(
        f"{PANEL}/usuarios", data={"username": "Pepe Luis", "role": "user", "mode": "link"}
    )
    assert response.status_code == 400
    assert "sin espacios" in response.text
    with auth_db.connection() as conn:
        assert auth_db.get_user(conn, "Pepe Luis") is None
