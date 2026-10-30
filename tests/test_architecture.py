"""Regla de dependencia de la arquitectura (hexagonal + clean).

El dominio no puede importar frameworks, drivers ni capas externas: es la regla
que mantiene el núcleo testeable sin infraestructura.
"""

from __future__ import annotations

import ast
from pathlib import Path

DOMAIN_DIR = Path(__file__).resolve().parents[1] / "src" / "kavos_lake" / "domain"

FORBIDDEN_ROOTS = {
    "fastapi",
    "starlette",
    "duckdb",
    "psycopg",
    "pyodbc",
    "jinja2",
    "fastmcp",
    "sqlalchemy",
    "pydantic",
    "pydantic_settings",
    "mcp",
}
FORBIDDEN_PREFIXES = ("kavos_lake.adapters", "kavos_lake.infrastructure")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def test_dominio_no_importa_frameworks_ni_drivers():
    offenders = [
        (path.name, module)
        for path in DOMAIN_DIR.glob("*.py")
        for module in _imports(path)
        if module.split(".")[0] in FORBIDDEN_ROOTS
    ]
    assert not offenders, f"el dominio importa infraestructura: {offenders}"


def test_src_no_importa_los_shims_de_api():
    """Ningún módulo de src/ puede depender de los shims de api/.

    Eso invertiría la dirección de la arquitectura y sólo funcionaría por
    accidente mientras los shims existan.
    """
    src_dir = Path(__file__).resolve().parents[1] / "src"
    offenders = [
        (path.relative_to(src_dir).as_posix(), module)
        for path in src_dir.rglob("*.py")
        for module in _imports(path)
        if module == "api" or module.startswith("api.")
    ]
    assert not offenders, f"src/ importa los shims de api: {offenders}"


def test_dominio_no_depende_de_adaptadores_ni_infraestructura():
    offenders = [
        (path.name, module)
        for path in DOMAIN_DIR.glob("*.py")
        for module in _imports(path)
        if module.startswith(FORBIDDEN_PREFIXES)
    ]
    assert not offenders, f"el dominio depende de capas externas: {offenders}"
