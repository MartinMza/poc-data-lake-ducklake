## ADDED Requirements

### Requirement: Esquema de origen simulado relacional
El sistema SHALL proveer un esquema de ejemplo en `postgres-source` con 3–5 tablas relacionadas (ej. clientes, pagos, unidades) con claves primarias y foráneas que simulen el origen Aurora.

#### Scenario: Esquema creado con integridad referencial
- **WHEN** se aplica el DDL documentado sobre `postgres-source` vacío
- **THEN** existen las tablas documentadas y las FKs rechazan filas huérfanas

### Requirement: Generador sintético configurable
El sistema SHALL proveer un generador de datos sintéticos, sin datos reales ni credenciales, con volumen configurable (nº de filas o factor de escala) y ejecución repetible.

#### Scenario: Generar volumen base
- **WHEN** el operador ejecuta el generador con el perfil por defecto
- **THEN** las tablas se pueblan con el volumen esperado y el generador reporta filas insertadas por tabla

#### Scenario: Escalar volumen
- **WHEN** el operador ejecuta el generador con un factor mayor (ej. perfil 10 GB)
- **THEN** el volumen crece de forma proporcional sin violar FKs ni requerir cambios de DDL

### Requirement: Columnas de incrementalidad
Cada tabla replicable SHALL exponer una columna de actualización (`updated_at` o equivalente) mantenida por el generador y por updates posteriores, apta como cursor incremental.

#### Scenario: Updates mueven el cursor
- **WHEN** el generador aplica una tanda de updates/inserts
- **THEN** las filas afectadas quedan con `updated_at` mayor al cursor previo y son detectables con un filtro `> cursor`
