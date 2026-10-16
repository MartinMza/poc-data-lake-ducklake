## ADDED Requirements

### Requirement: Backup y restore probados
El sistema SHALL proveer scripts de backup (dump del catálogo `postgres-catalog` + sync/copia del bucket Garage) y restore, con un procedimiento documentado y probado que recupere bronze/silver/gold tras borrar ambos componentes.

#### Scenario: Restore converge
- **WHEN** se hace backup, se destruyen catálogo y bucket de prueba y se ejecuta el restore
- **THEN** las tablas bronze/silver/gold vuelven a ser legibles con el mismo conteo documentado previo al borrado

### Requirement: Time travel y snapshots
El sistema SHALL demostrar time travel / snapshot de al menos una tabla (viaje a snapshot previo tras un update/delete) con el procedimiento y resultado documentados.

#### Scenario: Viaje a snapshot previo
- **WHEN** se registra un snapshot, se modifica la tabla y se consulta el snapshot anterior
- **THEN** la consulta histórica devuelve los datos previos a la modificación, con la sentencia exacta documentada

### Requirement: Concurrencia sin corrupción
El sistema SHALL demostrar dos lecturas concurrentes durante una escritura sin corrupción ni errores de catálogo (resultado documentado del experimento).

#### Scenario: Lectura durante escritura
- **WHEN** dos procesos leen (ej. queries gold) mientras un tercero escribe en bronze/silver
- **THEN** ambas lecturas completan con datos consistentes (snapshot válido) y la escritura commitea sin errores

### Requirement: Benchmark y documento de hallazgos
El sistema SHALL publicar un benchmark con tiempos de ingesta y 3 queries típicas sobre datos sintéticos escalados (perfiles 10 GB y 50 GB) y un documento de hallazgos con limitaciones, riesgos y recomendación explícita de avanzar o no.

#### Scenario: Resultados reproducibles y decisión
- **WHEN** se siguen los pasos del README de benchmark sobre un entorno limpio
- **THEN** se obtienen tiempos por fase/query comparables a los publicados y el doc de hallazgos contiene recomendación fundamentada

### Requirement: Capa de servicio opcional decidida
El proyecto SHALL decidir y documentar si se exporta gold a una base Postgres para consumo BI; si se decide que sí, SHALL proveer el job de export con refresco idempotente.

#### Scenario: Decisión trazable
- **WHEN** se lee el documento de hallazgos
- **THEN** consta la decisión (sí/no) con criterio (compatibilidad BI, latencia, costo operativo) y, en caso afirmativo, el job de export existe y un re-run no duplica filas en el destino
