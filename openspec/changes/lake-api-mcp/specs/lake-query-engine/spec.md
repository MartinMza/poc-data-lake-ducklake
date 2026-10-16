## ADDED Requirements

### Requirement: Introspección completa del catálogo DuckLake
El motor de consulta SHALL conectarse al catálogo DuckLake (`lake`) y listar todas las tablas disponibles en los esquemas `bronze`, `silver` y `gold` junto con su conteo de filas, así como describir la estructura de columnas y una muestra de datos de cualquier tabla válida.

#### Scenario: Listado de tablas de las tres capas
- **WHEN** se solicita el catálogo de tablas del lago
- **THEN** devuelve las tablas de `bronze` (Postgres y MSSQL), `silver` y `gold` con su esquema y cantidad de filas actual

#### Scenario: Descripción de esquema y muestra de tabla
- **WHEN** se solicita describir una tabla existente como `gold.gold_monthly_revenue` o `bronze.mssql_bs_operacion`
- **THEN** devuelve la lista de columnas con sus tipos de datos, el conteo total de filas y una muestra de hasta 5 filas serializadas en JSON

### Requirement: Consultas curadas de negocio y operativas
El motor SHALL ofrecer funciones predefinidas para consultar los modelos de negocio de `gold` (`gold_monthly_revenue`, `gold_customer_revenue`, `gold_unit_performance`) y búsquedas filtradas sobre las tablas de `silver` y `bronze` sin requerir que el consumidor escriba SQL.

#### Scenario: Consulta de KPI de ingresos mensuales
- **WHEN** se invoca la consulta curada de ingresos mensuales (con filtro opcional de moneda)
- **THEN** retorna los registros de `lake.gold.gold_monthly_revenue` ordenados cronológicamente

### Requirement: Sandbox SQL solo-lectura con límite de filas
El motor SHALL permitir ejecutar consultas SQL ad-hoc sobre el DuckLake únicamente si son de lectura (`SELECT`, `WITH`, `DESCRIBE`, `EXPLAIN`), rechazando cualquier sentencia de escritura, DDL o administración, y aplicando un límite máximo de filas (`limit` por defecto 100, tope 1000).

#### Scenario: Consulta SELECT válida con soporte de Time Travel
- **WHEN** se ejecuta un `SELECT` analítico sobre `lake.bronze.*`, `lake.silver.*` o `lake.gold.*` (incluyendo cláusulas `AT (VERSION => N)`)
- **THEN** devuelve las columnas, las filas resultantes (hasta el tope permitido) y el total de filas retornadas

#### Scenario: Bloqueo de sentencia destructiva o mutación
- **WHEN** se intenta ejecutar una consulta que contiene `DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`, `CREATE`, `COPY`, `ATTACH` o múltiples sentencias separadas por `;`
- **THEN** el motor rechaza la ejecución antes de enviarla a DuckDB y devuelve un error explícito de violación de solo-lectura
