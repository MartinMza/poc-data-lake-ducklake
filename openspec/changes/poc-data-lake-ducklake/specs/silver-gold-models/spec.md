## ADDED Requirements

### Requirement: Compatibilidad dbt-duckdb + DuckLake verificada
El proyecto SHALL documentar, contra la documentación actual de `dbt-duckdb`, si el adaptador soporta DuckLake (attach al catálogo) y la forma de configurarlo (`profiles.yml` / `ducklake` extension); si no está soportado, SHALL documentar el workaround (ej. vistas sobre Parquet, stage intermedio) antes de modelar.

#### Scenario: Configuración documentada y funcional
- **WHEN** se ejecuta `dbt debug` con el perfil del PoC
- **THEN** la conexión resuelve contra el DuckLake (catálogo + storage) y el documento indica versión de `dbt-core`/`dbt-duckdb` y enlace a la doc consultada

### Requirement: Modelos silver con limpieza y dedup
El proyecto SHALL proveer modelos silver (un modelo por entidad bronze) que apliquen tipado, normalización, limpieza de nulos/formatos y deduplicación por clave de negocio quedándose con la fila más reciente.

#### Scenario: Silver sin duplicados
- **WHEN** bronze contiene duplicados de una clave de negocio y se ejecuta `dbt run --select silver`
- **THEN** el modelo silver resultante contiene una fila por clave (la de mayor `updated_at`) con los tipos documentados

### Requirement: Modelos gold agregados
El proyecto SHALL proveer 2–3 modelos gold con agregados de negocio documentados (ej. facturación por cliente/mes, mora por unidad) construidos solo desde silver.

#### Scenario: Gold reconciliable
- **WHEN** se ejecuta `dbt run --select gold` sobre un silver conocido
- **THEN** los agregados coinciden con un cálculo de control documentado en el README del proyecto dbt

### Requirement: Tests de datos básicos
Cada modelo silver/gold SHALL tener tests `not_null` y `unique` sobre claves, y al menos un test `relationships` entre silver y su bronze/gold correspondiente, ejecutables con `dbt test`.

#### Scenario: Tests en verde y en rojo
- **WHEN** se ejecuta `dbt test` sobre datos válidos y luego se inyecta una fila inválida en el entorno de prueba
- **THEN** el primer run pasa todos los tests y el segundo falla en el test correspondiente
