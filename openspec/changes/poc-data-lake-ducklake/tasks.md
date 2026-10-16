## 1. Verificación de compatibilidad (verify-first)

- [x] 1.1 Verificar soporte DuckLake en `dlt` contra docs actuales y registrar versión, resultado y enlace; definir workaround si no hay destino nativo
- [x] 1.2 Verificar soporte DuckLake en `dbt-duckdb` contra docs actuales y registrar versión, configuración y enlace; definir workaround si no hay attach soportado
- [x] 1.3 Fijar la matriz de versiones (imágenes y paquetes) a partir de los hallazgos de 1.1–1.2

## 2. Infraestructura lakehouse

- [x] 2.1 Crear `docker-compose.yml` con `postgres-catalog`, `postgres-source`, `garage` y `prefect-server` (versiones fijadas, puertos y healthchecks documentados)
- [x] 2.2 Implementar script de bootstrap idempotente (bucket + credenciales S3 + base del catálogo)
- [x] 2.3 Implementar y pasar el smoke test DuckDB → DuckLake (crear/escribir/leer tabla de prueba)

## 3. Datos de origen simulados

- [ ] 3.1 Crear DDL de `postgres-source` (3–5 tablas con PK/FK, incluyendo `updated_at`)
- [ ] 3.2 Implementar generador sintético con volumen configurable y tanda de updates que mueva `updated_at`
- [ ] 3.3 Sembrar volumen base y validar integridad referencial y detección por cursor

## 4. Ingesta bronze (dlt)

- [ ] 4.1 Implementar pipeline `postgres-source` → `bronze` en DuckLake con incremental por `updated_at` y metadatos de ingesta
- [ ] 4.2 Validar carga inicial + segunda corrida incremental (solo cambios, sin duplicados)
- [ ] 4.3 Validar idempotencia ante reintento (mismo cursor, sin duplicados, cursor avanza solo al éxito)

## 5. Modelos silver y gold (dbt)

- [ ] 5.1 Crear proyecto dbt-duckdb con `profiles.yml` contra DuckLake y `dbt debug` en verde
- [ ] 5.2 Implementar modelos silver (tipado, limpieza, dedup por clave quedando la fila más reciente)
- [ ] 5.3 Implementar 2–3 modelos gold solo desde silver con cálculo de control documentado
- [ ] 5.4 Agregar y pasar tests `not_null`, `unique` y `relationships` (`dbt test`), incluyendo prueba en rojo con fila inválida

## 6. Orquestación (Prefect)

- [ ] 6.1 Implementar flow ingesta → `dbt run` → `dbt test` con orden, parada ante fallo, reintentos y config por env
- [ ] 6.2 Registrar/desplegar el flow en `prefect-server` local y validar ejecución y logs por intento

## 7. Operación, benchmark y cierre

- [x] 7.1 Implementar scripts de backup (dump catálogo + sync bucket) y restore; probar ciclo destruir → restaurar con conteos iguales
- [x] 7.2 Probar y documentar time travel / snapshot de una tabla (sentencia exacta y resultado)
- [x] 7.3 Ejecutar prueba de concurrencia (2 lecturas durante 1 escritura) y documentar resultado
- [x] 7.4 Ejecutar benchmark (ingesta + 3 queries, perfiles 10 GB y 50 GB) y publicar tiempos con hardware documentado
- [x] 7.5 Decidir y documentar capa de servicio gold → Postgres; si aplica, implementar export idempotente
- [x] 7.6 Redactar README (setup, arquitectura, resultados) y documento de hallazgos con recomendación de avanzar o no
