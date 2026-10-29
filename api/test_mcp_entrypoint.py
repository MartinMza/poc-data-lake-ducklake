"""`python -m api.mcp_server` tiene que seguir arrancando el servidor stdio.

Al correr con `-m`, el shim se ejecuta como `__main__`; el guard del módulo real
no se dispara porque su `__name__` es el del paquete. El guard vive en el shim.
"""

from __future__ import annotations

from pathlib import Path

import api.mcp_server

SHIM = Path(__file__).resolve().parent / "mcp_server.py"


def test_expone_el_servidor_mcp():
    assert api.mcp_server.mcp is not None
    assert hasattr(api.mcp_server.mcp, "run")


def test_el_shim_conserva_el_arranque_por_stdio():
    source = SHIM.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    assert "mcp.run(" in source
