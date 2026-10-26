"""Configuración común de la suite: credenciales de auth deterministas."""

from __future__ import annotations

import os

# Se aplican antes de importar `api.main` (que valida fail-closed al importarse).
os.environ.setdefault("AUTH_API_KEYS", "test-key-1,test-key-2")
os.environ.setdefault("AUTH_JWT_SECRET", "test-jwt-secret-0123456789abcdef0123456789abcdef")
os.environ.setdefault("AUTH_USERS", "admin:test-password,ops:test-password-2")
