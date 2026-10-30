"""`python -m dulse.adapters.inbound.mcp.server` arranca el servidor stdio.

El módulo real conserva el guard de `__main__`, así el transporte stdio para
Claude Desktop y similares sigue funcionando sin los shims de `api/`.
"""

from __future__ import annotations

from pathlib import Path

from dulse.adapters.inbound.mcp import server as mcp_server

SERVER = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "dulse"
    / "adapters"
    / "inbound"
    / "mcp"
    / "server.py"
)


def test_expone_el_servidor_mcp():
    assert mcp_server.mcp is not None
    assert hasattr(mcp_server.mcp, "run")


def test_el_modulo_conserva_el_arranque_por_stdio():
    source = SERVER.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    assert "mcp.run(" in source
