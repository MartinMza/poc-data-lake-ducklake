## ADDED Requirements

### Requirement: Compose stack reproducible en local
El sistema SHALL proveer un `docker-compose.yml` que levante `postgres-catalog`, `postgres-source`, `garage` y `prefect-server` con `docker compose up`, sin dependencias de servicios cloud y con versiones fijadas en el propio compose.

#### Scenario: Levantar stack desde cero
- **WHEN** un operador ejecuta `docker compose up -d` en un checkout limpio
- **THEN** los cuatro servicios quedan en estado healthy/running y exponen sus puertos documentados

#### Scenario: Versiones fijadas
- **WHEN** se inspecciona `docker-compose.yml` y los lockfiles asociados
- **THEN** cada imagen y paquete lleva tag/versión explícita (sin `latest`) y el README indica cómo actualizarlos

### Requirement: Bootstrap idempotente del lakehouse
El sistema SHALL proveer un script de bootstrap que cree el bucket en Garage, las credenciales S3 y la base/esquema del catálogo DuckLake en `postgres-catalog`, y que pueda re-ejecutarse sin error ni duplicados.

#### Scenario: Bootstrap inicial
- **WHEN** el operador ejecuta el comando de bootstrap documentado sobre el stack levantado
- **THEN** el bucket existe, las credenciales S3 funcionan y la base del catálogo acepta conexiones

#### Scenario: Bootstrap re-ejecutado
- **WHEN** el operador ejecuta el bootstrap por segunda vez sin cambios
- **THEN** el script termina con éxito y no crea buckets, usuarios ni objetos duplicados

### Requirement: Verificación de attach DuckLake
El sistema SHALL incluir una verificación de que DuckDB adjunta el DuckLake (catálogo en `postgres-catalog` + data path en Garage) y puede crear, escribir y leer una tabla de prueba.

#### Scenario: Smoke test DuckLake
- **WHEN** se ejecuta la verificación documentada (script o snippet SQL)
- **THEN** una tabla de prueba se crea en el catálogo, se inserta al menos una fila y una lectura posterior devuelve esa fila
