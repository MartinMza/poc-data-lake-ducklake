"""Guardas de deriva entre el código y la documentación.

Dos drifts ocurrieron de verdad y no los detectó ningún test:

1. Se agregaron variables de adquisición que el código leía y la documentación
   no mencionaba. Peor: el asistente de entorno las perdía porque no estaban en
   la plantilla.
2. La documentación siguió afirmando "7 tablas / 485 557 filas" después de que
   el catálogo pasó a ser completo (788 tablas / 23 225 319 filas). Diez lugares
   describían un lago que ya no existía.

Estos tests no validan prosa: validan que no vuelva a haber un dato huérfano.
Los conteos reales se consultan (`scripts/lake_counts.py`, `GET
/api/v1/sqlserver/tables`), no se citan de memoria, porque cambian con cada
backup.
"""

from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
MANUAL = RAIZ / "docs" / "MANUAL.md"

# Documentos que describen el estado actual. `docs/findings*.md` queda afuera a
# propósito: son registros fechados y sus números son correctos para su fecha.
DOCS_VIGENTES = (RAIZ / "README.md", MANUAL, RAIZ / "docs" / "API.md")

_GETENV = re.compile(r"os\.getenv\(\s*[\"']([A-Z0-9_]+)[\"']")


def _fuentes() -> list[Path]:
    base = RAIZ / "orchestration"
    return [
        base / "flow.py",
        base / "serve.py",
        *sorted((base / "kavos_acquire").glob("*.py")),
    ]


def _variables_leidas(prefix: str) -> set[str]:
    claves: set[str] = set()
    for archivo in _fuentes():
        claves |= set(_GETENV.findall(archivo.read_text(encoding="utf-8")))
    return {clave for clave in claves if clave.startswith(prefix)}


def test_toda_variable_de_adquisicion_que_lee_el_codigo_esta_documentada():
    leidas = _variables_leidas("ACQUIRE_")
    assert leidas, "el código debería leer variables ACQUIRE_*"

    documentadas = MANUAL.read_text(encoding="utf-8")
    faltan = sorted(clave for clave in leidas if clave not in documentadas)

    assert not faltan, (
        f"documentá en docs/MANUAL.md: {', '.join(faltan)}. "
        f"Una variable que el código lee y nadie documenta es la que después "
        f"falta en el .env y deja el pipeline corriendo sin hacer nada."
    )


def test_los_docs_no_citan_conteos_que_cambian_con_cada_backup():
    """El catálogo no tiene allowlist: los conteos fijos son deriva garantizada."""
    obsoletos = ("485 557", "485.557", "7 tablas", "siete tablas")

    for documento in DOCS_VIGENTES:
        texto = documento.read_text(encoding="utf-8")
        encontrados = [token for token in obsoletos if token in texto]
        assert not encontrados, (
            f"{documento.name} cita {encontrados}. El catálogo es completo y los "
            f"conteos cambian con cada backup: apuntá a scripts/lake_counts.py o "
            f"a GET /api/v1/sqlserver/tables en vez de hardcodearlos."
        )


def test_el_manual_documenta_la_regla_de_rebuild():
    """Recrear el contenedor sin rebuild dejó corriendo código viejo una vez."""
    assert "build" in MANUAL.read_text(encoding="utf-8")
    assert "force-recreate" in MANUAL.read_text(encoding="utf-8")
