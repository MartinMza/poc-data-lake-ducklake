"""Compatibilidad: la implementación vive en `dulse.adapters.outbound.duckdb_engine`.

Este archivo existe para no romper entrypoints, imágenes ni el proceso de tests
mientras la arquitectura se consolida (uvicorn api.main:app, scripts,
`python -m api.mcp_server`). Re-exporta el módulo real y se reemplaza a sí mismo
en sys.modules, así el estado mutable (singletons) es el mismo objeto.
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_impl = importlib.import_module("dulse.adapters.outbound.duckdb_engine")

globals().update({_k: _v for _k, _v in vars(_impl).items() if not _k.startswith("__")})
sys.modules[__name__] = _impl
