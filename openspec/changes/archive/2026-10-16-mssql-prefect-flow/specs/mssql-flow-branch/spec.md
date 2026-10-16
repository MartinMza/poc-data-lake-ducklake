## ADDED Requirements

### Requirement: Rama MSSQL en paralelo a postgres
El flow SHALL ejecutar la ingesta MSSQL (path rápido `fast_mssql.py` con tablas/ventana por env) en paralelo a la ingesta postgres, y SHALL correr `dbt run`/`dbt test` solo si ambas ramas terminan en éxito.

#### Scenario: Día normal ambas en verde
- **WHEN** ambas fuentes están disponibles
- **THEN** las dos ramas completan y dbt corre 7/7 con tests en verde

#### Scenario: Falla MSSQL detiene dbt
- **WHEN** la rama MSSQL falla (ej. instancia caída)
- **THEN** `dbt run`/`dbt test` no se ejecutan y el flow queda Failed con el error de la rama origen

### Requirement: Paridad operativa con tareas existentes
Cada tarea nueva SHALL tener los mismos reintentos por env, logs por intento visibles en server y ejecución como subproceso con venv verificado, igual que las tareas actuales.

#### Scenario: Reintento visible
- **WHEN** la ingesta MSSQL falla transitoriamente y se recupera
- **THEN** el server muestra los intentos con sus logs y el éxito final
