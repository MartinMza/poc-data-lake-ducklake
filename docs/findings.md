# Hallazgos PoC Data Lake (DuckLake + DuckDB) — decisión de avanzar o no

Fecha: 2026-10-08. Alcance: change `poc-data-lake-ducklake`, grupo 7
(operación, benchmark y cierre). Datos 100% sintéticos, stack local.

## 7.1 Backup / restore — PASS

Scripts: `scripts/backup.sh` (pg_dump `-Fc` del catálogo + espejo del bucket
vía `s3fs` + `counts.json` + `manifest.json`) y `scripts/restore.sh`
(recreate + `pg_restore` + purge/re-subida + comparación de conteos, FAIL si
difieren). Sin `aws-cli` en el host: el sync S3 usa `s3fs` del venv de bronze
(ya fijado por `dlt[s3]`, sin deps nuevas).

Ciclo destruir → restaurar ejecutado 2026-10-08:

| Fase | Resultado |
|---|---|
| Backup `backups/ops-baseline` | `catalog.dump` 123 358 B; 69 ficheros / 727 399 B del bucket; conteos bronze 100/50/200/1309, silver idénticos, gold 86/81/50 |
| Destruir | `DROP DATABASE ducklake_catalog` + purge de los 69 objetos; `bronze.payments` pasa a error `schema "bronze" does not exist` (lago ilegible, como se espera) |
| Restore | sha256 OK, `pg_restore` OK, 69 ficheros re-subidos; **14/14 tablas con conteo idéntico** al backup (incluye `_dlt_loads/_dlt_pipeline_state/_dlt_version`: el estado dlt viaja con el lago) |

Nota: el backup cubre el lago (catálogo + objetos). `gold_serving`
(Postgres, §7.5) es derivado y se re-exporta en un comando (§7.5), por
diseño no forma parte del dump.

## 7.2 Time travel / snapshot — PASS

`scripts/timetravel_check.py` (tabla propia `ops_timetravel_demo`, no toca
datos validados). Secuencia exacta ejecutada (snapshots reales del lago):

```sql
-- V0 = 142 (snapshot corriente antes del experimento)
CREATE TABLE ops_timetravel_demo (id INTEGER, name VARCHAR, amount DECIMAL(12,2));
INSERT INTO ops_timetravel_demo VALUES
  (1, 'alquiler-ene', 45000.00), (2, 'alquiler-feb', 47000.00), (3, 'alquiler-mar', 47000.00);
-- V1 = 144 (tras INSERT)
DELETE FROM ops_timetravel_demo WHERE id = 3;
UPDATE ops_timetravel_demo SET amount = 99999.00 WHERE id = 2;
-- V2 = 146 (tras DELETE+UPDATE)
SELECT * FROM lake.main.ops_timetravel_demo AT (VERSION => 144) ORDER BY id;
-- → 3 filas originales (id=2 con 47000.00), mientras la lectura actual da
--   2 filas (id=2 con 99999.00). La tabla se dropeó al final; los Parquet
--   históricos quedan (inmutables por diseño).
```

Sintaxis a fijar (verificada contra DuckLake 1.0 + duckdb 1.5.6; la doc
oficial usa el alias del catálogo, no `esquema.tabla`):
`FROM ducklake_snapshots('lake')` (lista), `FROM ducklake_current_snapshot('lake')`
(actual) y `<tabla> AT (VERSION => N)` / `AT (TIMESTAMP => ...)`.

## 7.3 Concurrencia — PASS

`scripts/concurrency_check.py`: 3 procesos separados contra el mismo
catálogo Postgres (multi-proceso real). 2026-10-08:

```
reader-1: status=ok ops=90 t=0.62s   (30 iter × 3 queries gold)
reader-2: status=ok ops=90 t=0.61s
writer:   status=ok ops=20 t=1.18s   (20 INSERTs con commit por fila)
conteo final ops_concurrency_demo: 20 (esperado 20)
```

180 lecturas completan sin errores de catálogo con valores de snapshots
válidos; la escritura commitea sus 20 filas; conteo final exacto, sin
corrupción. Alcance: el escritor usa tabla propia; no se probó escritura
concurrente sobre la misma tabla bronze (fuera del criterio: el flow
orquesta una sola escritura a la vez).

## 7.4 Benchmark — medido (demo/base/mid) + 10/50 GB extrapolados

`scripts/benchmark.py`. Host documentado: 22 CPU, 32 GB RAM, NVMe con
200 GB libres; Garage nodo único en el mismo host; conexiones DuckDB frías;
ingesta = `CREATE TABLE bench.<t> AS FROM read_csv(...)` (path DuckDB→S3 +
commit de catálogo); limpieza de esquemas al terminar.

| Escala | Clientes | Filas | Gen. CSV | Ingesta | Throughput | Parquet | Q1 | Q2 | Q3 |
|---|---|---|---|---|---|---|---|---|---|
| demo | 100 | 1 540 | 0,03 s | 0,4 s | 3,9 k f/s¹ | 0,2 MB | 0,03 s | 0,13 s | 0,04 s |
| base | 10 000 | 155 333 | 1,0 s | 0,5 s | 311 k f/s | ~5 MB | 0,06 s | 0,14 s | 0,05 s |
| mid | 100 000 | 1 550 469 | 9,5 s | 1,0 s | 1,6 M f/s | ~52 MB | 0,13 s | 0,17 s | 0,05 s |
| 10gb² | 2 000 000 | ~31 M | ~190 s | ~20 s | — | ~1,1 GB | ~0,3 s | — | — |
| 50gb² | 10 000 000 | ~155 M | ~950 s | ~100 s | — | ~5,3 GB | ~1 s | — | — |

¹ demo dominado por costos fijos (attach/commit). ² Extrapolado (ver método).

Queries: Q1 `GROUP BY mes, moneda` sobre payments (92 grupos); Q2 top-10
clientes por monto con join payments→leases→customers y filtro 6 meses;
Q3 lookup puntual por `payment_id`. Q1 escala ~2× por cada 10× filas
(columnar Parquet); Q2/Q3 casi planas en este rango.

Método de extrapolación (lineal desde mid, 162 900 filas/s en generación y
1,6 M filas/s en ingesta; Parquet = 0,33 × CSV medido en base y mid):
explícitamente estimaciones, no SLA. **No se materializaron 10/50 GB**:
`generate.py` construye todas las filas en memoria (`--scale 50gb` =
~155 M dicts ≈ decenas de GB de RAM → OOM probable en 32 GB; además ~16 min
solo de generación). El generador necesita modo streaming para esos
volúmenes (limitación §Limitaciones). El path dlt completo no se
re-cronometró a escala para no destruir el estado bronze validado; la
ingesta directa es proxy del costo dominante (escritura al lago).

Lectura clave: el perfil "50 GB" nominal produce **~5 GB reales en Parquet**
(compresión columnar 0,33× sobre CSV de ~103 B/fila). El objetivo < 300 GB
cabe con holgura en un solo nodo a estos ratios.

## 7.5 Capa de servicio gold → Postgres — decisión: SÍ

Criterio: (a) compatibilidad BI — protocolo Postgres universal
(Metabase/Superset/PowerBI/Grafana) frente a DuckDB embebido, que exige
driver ad-hoc por consumidor y no ofrece servidor concurrente en el PoC;
(b) latencia — gold son agregados pequeños (incluso a 50 GB serían miles de
filas), refresco despreciable; (c) costo operativo — sin infra nueva (misma
instancia `postgres-catalog`, base nueva `gold_serving`), refresco
transaccional. En contra: tercera copia + staleness (el export corre tras
`dbt test` en el flow; documentado, no medido como SLO).

Implementación: `scripts/export_gold.py` (idempotente por swap atómico:
escribe `t_next` y hace `DROP t + RENAME` en una transacción; re-correr
reemplaza, nunca duplica). Verificado 2 corridas: 81/86/50 filas =
conteos del lago; sumas Postgres = lago al centavo
(87 465 038,10 en los tres modelos; 1309 pagos / 200 contratos).

## Fix incluido: `source/generate.py --mode seed --out postgres`

Fallaba siempre (`INSERT` con IDs explícitos sobre `GENERATED ALWAYS AS
IDENTITY` → `psycopg.errors.GeneratedAlways`); sin el fix el setup no es
reproducible. Fix mínimo aplicado en `write_postgres()`: `OVERRIDING SYSTEM
VALUE` + `setval()` de las 4 secuencias (si no, los `INSERT` sin ID del
`--mode update` colisionan en 1). Verificado en base scratch (seed 100 →
FK huérfanas 0, secuencia en 1191, tanda update detectada por cursor
10/178, igual que la evidencia de bronze). No se tocó `ddl.sql` ni el estado
validado de `source_db`.

## Fricciones conocidas (registradas, no bloqueantes)

1. **Catálogo 0.3→1.0**: `duckdb>=1.5.2` exigido por DuckLake 1.0; todos los
   venvs ya unificados en `duckdb==1.5.6` (incl. `scripts/`, que lo documenta
   en su README). No migrar el catálogo unilateralmente: es estado compartido.
2. **Replace/swap en bronze visible a consumidores**: `--full-refresh` (drop
   + recarga) deja ventana sin datos; los reintentos dlt re-ejecutan la carga
   completa. Para servicio, consumir gold vía export con swap (§7.5), no
   bronze directo.
3. **Prefect server 3.6.22 vs cliente 3.8.8**: solo warning de versión; flow
   runs tracked y visibles en API/UI sin otro efecto (ver
   `orchestration/README.md`). Alinear pins al subir a staging.
4. **`dbt/profiles.yml` con hosts fijos** (`localhost:5433`, endpoint S3):
   deuda documentada en `orchestration/README.md`; si el lago cambia de host,
   evaluar `env_var()` en el perfil (implica tocar `dbt/`, coordinar dueño).

## Limitaciones y riesgos

1. DuckLake 1.0 (abr-2026) + ecosistema joven: API cambiante (la mig
...[truncated 2086 chars]
