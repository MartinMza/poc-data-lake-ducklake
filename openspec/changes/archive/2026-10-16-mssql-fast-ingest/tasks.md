## 1. Línea base

- [x] 1.1 Instrumentar y publicar tiempos por fase (restore, extract dlt, merge) y por tabla sobre la DB real

## 2. Path rápido

- [x] 2.1 Implementar export `bcp` paralelo por tabla con grado configurable
- [x] 2.2 Implementar carga `COPY` a `staging_mssql` preservando tipos
- [x] 2.3 Implementar `MERGE` staging → bronze con columnas de metadatos compatibles y validación de paridad (conteos + sumas + dbt tests)

## 3. Ventana temporal

- [x] 3.1 Parametrizar ventana por tabla (`N` días por env) y modo resync full; validar convergencia ventana-semana + resync

## 4. Cierre

- [x] 4.1 Publicar speedup antes/después, declarar path principal por tabla y documentar en `bronze/README.md` + findings
