## ADDED Requirements

### Requirement: Servidor MCP con FastMCP para agentes de IA
El servicio SHALL implementar un servidor **FastMCP** que exponga herramientas (`list_tables`, `describe_table`, `get_kpi`, `run_query`, `list_snapshots`) y recursos de catálogo sobre el DuckLake, ejecutable tanto por transporte `stdio` como montado por HTTP/SSE en la aplicación FastAPI.

#### Scenario: Descubrimiento e invocación de tools MCP
- **WHEN** un cliente MCP (por `stdio` o HTTP/SSE) solicita las herramientas disponibles e invoca `list_tables` o `describe_table`
- **THEN** el servidor FastMCP devuelve el catálogo completo (`bronze`, `silver`, `gold`) o el esquema detallado de la tabla solicitada

#### Scenario: Consulta analítica SQL desde un agente vía MCP
- **WHEN** un agente invoca la tool `run_query` pasando una consulta `SELECT` o `WITH` sobre cualquier capa del DuckLake
- **THEN** el servidor valida los guardrails read-only y retorna el resultado estructurado con columnas y filas limitadas
