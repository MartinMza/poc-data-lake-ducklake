## 1. Relevamiento del .bak (verify-first)

- [x] 1.1 Pedir al operador el `.bak` (o su `RESTORE HEADERONLY`): versión origen, tamaño, bases y lista de tablas con PKs y columnas de cursor
- [x] 1.2 Fijar imagen MSSQL exacta (CU) >= versión origen y confirmar arquitectura del host (flag `--platform` si es ARM)

## 2. Servicio MSSQL

- [x] 2.1 Agregar servicio `mssql` al compose (imagen fijada, password por env, healthcheck, volumen `./mssql-backup/` + gitignore)
- [x] 2.2 Validar login `sa` y visibilidad del `.bak` dentro del contenedor; documentar `MSSQL_SA_PASSWORD` en `.env.example`

## 3. Restore del backup

- [x] 3.1 Implementar script de restore (FILELISTONLY + RESTORE WITH MOVE dinámico) idempotente con verificación post-restore
- [x] 3.2 Ejecutar restore real, registrar tablas/renglones y decidir estrategia por tabla (incremental vs full-refresh + merge)

## 4. Ingesta a bronze (dlt)

- [x] 4.1 Instalar driver (pyodbc + ODBC 18, fallback pymssql) y crear `bronze/pipeline_mssql.py` al mismo lake con prefijo de tablas y metadatos de ingesta
- [x] 4.2 Validar carga inicial (conteos = origen, tipos críticos sin pérdida) y segunda corrida (solo delta o merge sin duplicados)

## 5. Cierre

- [x] 5.1 Documentar en `bronze/README.md` (o README del slice): ubicación del `.bak`, comando restore, estrategia por tabla y mapeo de tipos
- [x] 5.2 Registrar hallazgos (versión imagen, ARM si aplica, fricciones ODBC) para el doc de findings
