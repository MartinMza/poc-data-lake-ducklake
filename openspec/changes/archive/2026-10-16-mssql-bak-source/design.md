## Context

El PoC del datalake ingiere hoy solo desde `postgres-source` (datos sintéticos). El operador dispone además de un backup diario real en formato `.bak` de SQL Server que necesita cargar y volcar al datalake. El repo ya tiene el patrón resuelto para postgres (compose + bootstrap + pipeline dlt incremental + mismo DuckLake), por lo que este change lo replica para una segunda fuente MSSQL sin alterar lo existente.

Constraint clave: el `.bak` lo aporta el operador y no se commitea; su versión de SQL Server origen y su tamaño se conocen recién en implementación.

## Goals / Non-Goals

**Goals:**
- Servicio MSSQL local reproducible dentro del compose actual.
- Restore del `.bak` diario con un solo comando, idempotente y verificado.
- Ingesta dlt MSSQL → `bronze` del mismo DuckLake, sin duplicados entre corridas diarias.

**Non-Goals:**
- Modelos silver/gold sobre datos MSSQL (change futuro; bronze es el contrato de salida).
- Programar el restore diario o integrarlo al flow Prefect (se documenta el trigger manual; la automatización queda como follow-up).
- CDC / Change Tracking de SQL Server (el `.bak` diario es un snapshot completo; no hay log continuo).

## Decisions

### 1. Imagen: `mcr.microsoft.com/mssql/server:2022` fijada por CU
Restaura backups generados por 2022 y anteriores. Alternativas: 2019 (descartada salvo que el `.bak` sea antiguo y 2022 falle) y `latest` (descartado: rompe reproducibilidad). La versión exacta (CU) se fija en implementación tras conocer la versión origen del `.bak`; si el `.bak` es más nuevo que 2022, se sube de imagen.

### 2. Restore vía script Python (pyodbc) con `RESTORE FILELISTONLY` previo
El script consulta primero los nombres lógicos del backup y genera el `RESTORE DATABASE ... WITH MOVE` dinámicamente, en vez de hardcodear paths. Alternativas: `sqlcmd` (la imagen server no lo trae; exigiría instalar mssql-tools) y restore manual en SSMS (no reproducible). El script corre desde el host contra el puerto publicado, igual que el resto del tooling.

### 3. Driver: pyodbc + Microsoft ODBC Driver 18
Es el driver oficial y el que mejor mapea tipos nuevos (`DATETIME2`, `DECIMAL`). Alternativa `pymssql` (instalación más simple, sin ODBC) descartada como primera opción porque va rezagado en tipos y collations; queda como fallback si ODBC 18 complica el setup local. El venv de bronze suma la dependencia sin tocar el pipeline de postgres.

### 4. Pipeline dlt separado (`pipeline_mssql.py`) al mismo lake y esquema `bronze`
Un archivo nuevo en `bronze/` reutilizando el attach canónico (mismo catálogo + `DATA_PATH`) y la fuente `sql_database` de dlt con dialecto `mssql`. Alternativa (extender `pipeline.py`) descartada: mezclaría credenciales, cursores y dependencias de dos orígenes. Colisiones de nombres se evitan con prefijo de origen en las tablas bronze.

### 5. Incremental según lo que traiga el backup
Si las tablas tienen columna de cursor (`updated_at`/`rowversion`), incremental como en postgres; si no, full-refresh diario con `merge` por PK (el `.bak` es snapshot completo, así que el refresh converge sin duplicados). La decisión por tabla se toma tras inspeccionar el esquema restaurado y queda documentada; no se asume de entrada.

## Risks / Trade-offs

- [Imagen MSSQL solo `linux/amd64`; en hosts ARM requiere emulación (`--platform linux/amd64`), lenta pero funcional para PoC] → Mitigación: documentar el flag y medir el impacto en el benchmark si aplica.
- [Versión del `.bak` mayor que la imagen] → Mitigación: verificación previa (`RESTORE HEADERONLY`); mensaje explícito y subida de imagen.
- [Tablas sin PK ni cursor → ni merge ni incremental posibles] → Mitigación: full-refresh con `replace` documentado como fallback, advirtiendo costo a escala.
- [Tamaño del `.bak` (desconocido) puede exceder disco/RAM del host] → Mitigación: pedir tamaño antes de implementar; restore y primera carga se prueban con monitoreo de disco.
- [ODBC Driver 18 exige `TrustServerCertificate=yes` en local] → Mitigación: flag solo-PoC-local documentado, nunca en conexión productiva.

## Migration Plan

No hay migración (fuente nueva, nada existente cambia). Orden: servicio MSSQL → restore manual del primer `.bak` → inspección de esquema → pipeline → validación de segunda corrida. Rollback = `docker compose rm -sv mssql` + borrar tablas `bronze` prefijadas; el resto del lake intacto.

## Open Questions

- ¿Qué versión de SQL Server generó el `.bak` y cuánto pesa? (Define imagen exacta y viabilidad en el host.)
- ¿Las tablas tienen PK y alguna columna de cursor (`updated_at`, `rowversion`)? (Define incremental vs full-refresh por tabla.)
- ¿Arquitectura del host donde correrá (x86_64 o ARM)? (Define si hace falta emulación.)
- ¿El `.bak` contiene una o varias bases, y hay objetos fuera de tablas (vistas, SPs) que ignorar?
