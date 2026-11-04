"""Puerto del motor de consultas.

El adaptador de salida (DuckDB) lo implementa; la capa de entrada (HTTP, MCP)
solo depende de este contrato.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class QueryEnginePort(Protocol):
    """Consultas de solo lectura sobre el lago."""

    def list_tables(
        self, schemas: tuple[str, ...] = ("bronze",)
    ) -> list[dict[str, Any]]: ...

    def describe_table(self, schema: str, table: str) -> dict[str, Any]: ...

    def list_snapshots(self, limit: int = 10) -> list[dict[str, Any]]: ...

    def execute_query(self, sql: str, limit: int = 100) -> dict[str, Any]: ...

    def sqlserver_tables(self, schema: str = "bronze") -> list[dict[str, Any]]: ...

    def preview_rows(
        self, schema: str, table: str, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]: ...
