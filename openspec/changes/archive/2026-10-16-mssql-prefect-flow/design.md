## Context

`orchestration/flow.py` (`dulse-medallion`) corre hoy postgres-ingest → dbt-run → dbt-test secuencial. Existen y están validados: `scripts/restore_mssql.sh` (idempotente), `bronze/fast_mssql.py` (bcp→staging→MERGE, 12 s, paridad total) y la marca de estado dlt. Falta atarlos al schedule diario.

## Goals / Non-Goals

**Goals:**
- `python orchestration/flow.py` = día completo: restore si hay `.bak` nuevo + ambas ingestas + dbt, con un solo comando.
- Semántica de fallo idéntica a la actual (fail-stop, reintentos, logs).

**Non-Goals:**
- Modelos dbt sobre bronze MSSQL (no existen aún; dbt sigue validando solo postgres).
- Copiar el `.bak` desde fuera del host (el operador lo acerca a `./mssql-backup/` o define `MSSQL_BAK_PATH`).

## Decisions

### 1. Ramas postgres + MSSQL en paralelo, dbt como join
Son independientes (distintos orígenes y tablas). Prefect ejecuta ambas concurrentes y dbt espera a las dos. Alternativa (secuencial) descartada: suma tiempos sin necesidad; el benchmark ya calibró que MSSQL no satura el host en paralelo moderado.

### 2. Fast path como ingesta MSSQL del flow, dlt como fallback por env
`FAST_MSSQL_MODE=fast|dlt` (default `fast`, medido 5,4x). Alternativa (solo dlt) descartada por lenta para ventana diaria a escala.

### 3. Marca de restore en archivo local (JSON con path+mtime+size+DB)
Simple, sin estado en el server ni tabla extra. Se actualiza solo tras restore exitoso + verificación. Alternativa (siempre restaurar) descartada: 132 s diarios desperdiciados sin .bak nuevo.

## Risks / Trade-offs

- [Paralelismo postgres+mssql compite por DuckDB (catálogo serializa MERGEs)] → Mitigación: ya observado; reintentos absorben colisiones puntuales, y dbt corre después, nunca concurrente.
- [`.bak` a medio copiar (operador copiando mientras corre el flow)] → Mitigación: se compara size+mtime dos veces con espera breve; si cambia entre lecturas, se salta con warning.
- [Fast path falla y dlt fallback también lento] → Mitigación: el fallback existe pero si ambos fallan el flow falla explícito (mejor que media ingesta silenciosa).

## Migration Plan

Sin migración: extensión aditiva del flow. Validar e2e (con y sin `.bak` nuevo, con fallo MSSQL) y redeploy al server. Rollback = deploy anterior del flow (git revert de `orchestration/`).

## Open Questions

- ¿El `.bak` diario llega siempre con el mismo nombre o rotado por fecha? (Define si la marca compara nombre o solo mtime/size.)
- ¿Schedule del deployment (hora diaria) o solo trigger manual por ahora?
