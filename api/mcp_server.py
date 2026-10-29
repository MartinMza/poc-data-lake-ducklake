"""Compatibilidad: la implementación vive en `dulse.adapters.inbound.mcp.server`.

Este archivo existe para no romper `python -m api.mcp_server` (transporte stdio
para Claude Desktop, Cursor y similares) mientras la arquitectura se consolida.
Re-exporta el módulo real y se reemplaza a sí mismo en sys.modules, así el
estado mutable (el servidor MCP) es el mismo objeto.

El guard de `__main__` tiene que estar ACÁ: al correr con `-m` el módulo se
ejecuta como `__main__`, y el guard del módulo real (`__name__ == "__main__"`)
no se dispara porque su `__name__` es el del paquete.
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_impl = importlib.import_module("dulse.adapters.inbound.mcp.server")

globals().update({_k: _v for _k, _v in vars(_impl).items() if not _k.startswith("__")})
sys.modules[__name__] = _impl

if __name__ == "__main__":
    # Transporte local por stdio: corre como proceso del operador, sin pasar
    # por la autenticación HTTP.
    _impl.mcp.run(transport="stdio")
