## 1. Ramas del flow

- [x] 1.1 Agregar tarea `mssql-ingest` (fast path por env `FAST_MSSQL_MODE=fast|dlt`, tablas/ventana por env) en paralelo a postgres-ingest con dbt como join y fail-stop conjunto
- [x] 1.2 Agregar tarea `bak-watch-restore` (marca JSON path+mtime+size, restore condicional, skip con warning sin `MSSQL_BAK_PATH`, doble lectura anti-copia-parcial)

## 2. Validación

- [x] 2.1 Validar e2e día normal (sin .bak nuevo: skip + ambas ramas + dbt verde) y con `.bak` nuevo (restore + marca actualizada)
- [x] 2.2 Validar fallo MSSQL (dbt no corre, flow Failed con el error) y redeploy al prefect-server

## 3. Cierre

- [x] 3.1 Documentar en `orchestration/README.md` (env vars, schedule sugerido, rollback) y registrar hallazgos
