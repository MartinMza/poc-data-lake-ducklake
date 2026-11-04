"""Regresiones del rename Dulse -> KavosLake.

Protege el contrato de nombres: si alguien vuelve a mezclar el guión bajo del
paquete con el guión de los contenedores, o si un identificador de runtime se
queda con el nombre viejo, esto falla.
"""

from __future__ import annotations

import re
from pathlib import Path

from kavos_lake.adapters.inbound.http import panel
from kavos_lake.adapters.inbound.mcp import server
from kavos_lake.adapters.outbound import postgres_auth
from kavos_lake.infrastructure import security

ROOT = Path(__file__).resolve().parents[1]


def test_identificadores_de_runtime_usan_el_nombre_nuevo():
    assert security.jwt_issuer() == "kavos-lake-api"
    assert panel.COOKIE_NAME == "kavos_lake_session"
    assert postgres_auth.KEY_PREFIX == "kavos_lake_"
    assert server.mcp.name == "kavos-lake"


def test_base_de_credenciales_por_defecto(monkeypatch):
    monkeypatch.delenv("AUTH_DB_NAME", raising=False)
    assert postgres_auth.database_name() == "kavos_lake_auth"


def test_compose_no_tiene_nombres_malformados():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "kavos_lake-" not in compose, "mezcla guión bajo del paquete con guión"
    assert "kavos-lake-lake" not in compose, "nombre duplicado"


def test_backup_inspecciona_contenedores_que_compose_declara():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    backup = (ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
    containers = set(re.findall(r"container_name:\s*(\S+)", compose))
    inspected = set(re.findall(r"docker inspect (\S+)", backup))
    assert inspected, "backup.sh no inspecciona ningún contenedor"
    assert inspected <= containers, f"nombres desalineados: {inspected - containers}"


def test_flow_de_prefect_usa_el_nombre_nuevo():
    flow = (ROOT / "orchestration" / "flow.py").read_text(encoding="utf-8")
    assert 'FLOW_NAME = "kavos-lake-medallion"' in flow
