"""La configuración tiene una sola fuente: Settings.

Tres regresiones que no se ven en un test funcional:

- una lectura de entorno suelta en cualquier módulo (dos fuentes de verdad);
- una variable que docker-compose inyecta y que no corresponde a ningún campo:
  pydantic la ignora en silencio y el contenedor arranca con el default;
- una caché de configuración que sigue sirviendo valores viejos.
"""

from __future__ import annotations

import re
from pathlib import Path

from kavos_lake.infrastructure import config

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CONFIG_PATH = SRC / "kavos_lake" / "infrastructure" / "config.py"

# Compose las inyecta, pero no son configuración de la app: las consume el CMD
# de uvicorn (--host / --port).
NO_CONFIG_VARS = {"api_bind_host", "api_internal_port"}


def _lake_api_block() -> str:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    return compose.split("  lake-api:", 1)[1].split("  lake-init:", 1)[0]


def test_ningun_modulo_fuera_de_config_lee_el_entorno():
    offenders = []
    for path in SRC.rglob("*.py"):
        if path == CONFIG_PATH:
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "os.environ" in line or "os.getenv" in line:
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, f"lecturas de entorno fuera de config.py: {offenders}"


def test_la_configuracion_no_esta_cacheada():
    assert config.get_settings() is not config.get_settings()


def test_un_cambio_de_entorno_se_ve_sin_reset(monkeypatch):
    monkeypatch.setenv("LAKEHOUSE_BUCKET", "primero")
    assert config.get_settings().ducklake_data_path == "s3://primero/data/"
    monkeypatch.setenv("LAKEHOUSE_BUCKET", "segundo")
    assert config.get_settings().ducklake_data_path == "s3://segundo/data/"


def test_las_variables_de_compose_corresponden_a_campos():
    claves = {
        match.group(1).lower()
        for match in re.finditer(r"^      ([A-Z_]+):", _lake_api_block(), re.MULTILINE)
    }
    campos = set(config.Settings.model_fields)
    desconocidas = claves - campos - NO_CONFIG_VARS
    assert not desconocidas, (
        "compose pasa variables que Settings ignora (arrancaría con el default): "
        f"{sorted(desconocidas)}"
    )


def test_la_base_de_credenciales_tiene_variables_propias_en_compose():
    bloque = _lake_api_block()
    for var in (
        "AUTH_DB_HOST",
        "AUTH_DB_PORT",
        "AUTH_DB_USER",
        "AUTH_DB_PASSWORD",
        "AUTH_DB_NAME",
    ):
        assert var in bloque, f"falta {var} en el servicio lake-api"
