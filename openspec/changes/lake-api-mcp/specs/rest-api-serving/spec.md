## ADDED Requirements

### Requirement: Endpoints REST con FastAPI y documentación OpenAPI
El servicio SHALL exponer una aplicación **FastAPI** con endpoints HTTP JSON para healthcheck, catálogo de tablas, descripción de tabla, KPIs curados de la capa Gold y ejecución de consultas SQL read-only, con esquema OpenAPI autogenerado en `/docs`.

#### Scenario: Consulta HTTP de catálogo y detalle de tabla
- **WHEN** un cliente realiza `GET /api/v1/tables` o `GET /api/v1/tables/{schema}/{table}`
- **THEN** responde con HTTP 200 y el JSON del catálogo o esquema/muestra correspondiente (o HTTP 404 si la tabla no existe)

#### Scenario: Consulta HTTP de KPIs curados
- **WHEN** un cliente realiza `GET /api/v1/kpis/monthly-revenue`, `GET /api/v1/kpis/customer-revenue` o `GET /api/v1/kpis/unit-performance`
- **THEN** responde con HTTP 200 y los datos agregados de la capa `gold`

#### Scenario: Ejecución SQL por POST con validación HTTP
- **WHEN** un cliente envía `POST /api/v1/query` con `{"sql": "SELECT ...", "limit": 50}`
- **THEN** responde con HTTP 200 y las filas resultantes si el SQL es de lectura, o HTTP 400/403 si viola los guardrails de solo-lectura
