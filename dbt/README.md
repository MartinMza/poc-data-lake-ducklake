# Modelos silver y gold (dbt)

Slice `silver-gold-models` del change `poc-data-lake-ducklake` (tasks 5.1–5.4).
Proyecto dbt-duckdb contra el MISMO DuckLake del stack
(catálogo `postgres-catalog` + `DATA_PATH s3://lakehouse/data/` en Garage).

## Versiones (specs/compat-matrix.md §1.3, línea 1.x)

| Paquete | Pin | Motivo |
|---|---|---|
| `dbt-core` | `==1.12.5` | matriz §1.3 |
| `dbt-duckdb` | `==1.11.0` | matriz §1.3 (bloque `attach` + `secrets`) |
| `duckdb` | `==1.5.6` | matriz §1.3; exige catálogo DuckLake 1.0 (ver nota) |

Instalación y uso:

```bash
cd dbt
uv venv .venv && uv pip install -r requirements.txt   # o python3 -m venv
.venv/bin/dbt debug --profiles-dir .   # 5.1: debe dar "All checks passed!"
.venv/bin/dbt run --profiles-dir .     # 5.2+5.3: crea lake.silver.* y lake.gold.*
.venv/bin/dbt test --profiles-dir .    # 5.4: 28 tests en verde
```

`profiles.yml` resuelve el attach canónico (ver `scripts/smoke_ducklake.py`)
más el secret S3 con endpoint custom, que la matriz (§1.2, caveat 2) había
dejado sin smoke test: aquí queda verificado (`dbt debug` en verde con
`DATA_PATH s3://...` + `endpoint localhost:3900`). En el adapter los
`secrets` se crean ANTES del `ATTACH`, por eso el orden funciona.

> Nota de coordinación con bronze-ingestion: el lago se encontró primero en
> versión DuckLake 0.3 (creado por `scripts/` con duckdb 1.4.4, que solo lee
> ≤0.3) y durante este slice el otro workstream lo migró a 1.0. El pin pasó
> de `duckdb==1.4.4` a `==1.5.6` en consecuencia. No migrar el catálogo
> unilateralmente: es estado compartido. Sin macro propia el esquema sale
> `main_silver` (matriz §1.2 caveat 1); `macros/generate_schema_name.sql`
> lo corrige a `silver` / `gold` exactos (verificado en el lago).

## Modelos

Fuentes (`models/sources.yml`): `lake.<bronze_schema>.{customers,units,leases,payments}`.
El esquema bronze se resuelve por variable para validar con fixtures sin
tocar el `bronze` real (poblado en paralelo por el otro slice):

```bash
.venv/bin/dbt run --profiles-dir . --vars '{bronze_schema: bronze_fixture}'
```

`bronze_fixture` se cargó con `source/generate.py --mode seed --rows 100
--out csv` (100/50/200/1190 filas). Tras validar contra el `bronze` real,
el run por defecto (sin `--vars`) es el que vale; `bronze_fixture` queda
como andamio documentado y reproducible.

Silver (`models/silver/`, uno por entidad bronze): tipado explícito
(`BIGINT`, `DECIMAL(12,2)`, `TIMESTAMPTZ`, `DATE`), limpieza
(`trim`, `lower`/`upper`, `nullif`, `coalesce`, filtros de `amount > 0`,
monedas `UYU`/`USD`, estados conocidos, `end_date >= start_date`) y dedup
por clave de negocio quedando la fila de mayor `updated_at`
(`row_number() ... order by updated_at desc`, desempate por PK).

Gold (`models/gold/`, solo desde silver vía `ref()`, nunca desde bronze):

| Modelo | Grano | Agregado |
|---|---|---|
| `gold_monthly_revenue` | (mes, moneda) | `sum(amount)`, `count(*)` de `silver_payments` |
| `gold_customer_revenue` | cliente | `sum(amount)`, `count(*)`, min/max `paid_at` (vía `leases`) |
| `gold_unit_performance` | unidad | `n_leases`, `n_active_leases`, `total_collected` (vía `leases`) |

## Cálculo de control (criterio gold reconciliable)

```sql
-- Totales gold == totales silver (las 6 deben dar TRUE):
SELECT (SELECT sum(total_amount) FROM gold.gold_monthly_revenue)
     = (SELECT sum(amount) FROM silver.silver_payments);
SELECT (SELECT sum(n_payments) FROM gold.gold_monthly_revenue)
     = (SELECT count(*) FROM silver.silver_payments);
SELECT (SELECT sum(total_paid) FROM gold.gold_customer_revenue)
     = (SELECT sum(p.amount) FROM silver.silver_payments p
        JOIN silver.silver_leases l ON p.lease_id = l.lease_id);
SELECT (SELECT sum(total_collected) FROM gold.gold_unit_performance)
     = (SELECT sum(p.amount) FROM silver.silver_payments p
        JOIN silver.silver_leases l ON p.lease_id = l.lease_id);
SELECT (SELECT sum(n_leases) FROM gold.gold_unit_performance)
     = (SELECT count(*) FROM silver.silver_leases);
SELECT (SELECT count(*) FROM silver.silver_customers)
     = (SELECT count(DISTINCT customer_id) FROM silver.silver_customers);
```

## Tests (5.4) y evidencia en rojo

`models/properties.yml`: `not_null` + `unique` en las PK de los 4 silver y
en `customer_id`/`unit_id` de gold (+ `not_null` en resto de claves);
`relationships`: `leases.customer_id → customers`,
`leases.unit_id → units`, `payments.lease_id → leases`,
`gold_customer_revenue.customer_id → customers`,
`gold_unit_performance.unit_id → units`; más test singular
`tests/assert_gold_monthly_revenue_unique.sql` para la clave compuesta
(mes, moneda) de `gold_monthly_revenue`.

Evidencia: `dbt test` → 28/28 PASS; tras `INSERT` manual de un pago con
`lease_id = 999999` inexistente en `silver.silver_payments`,
`relationships_silver_payments_lease_id...` → FAIL (1 fila); retirada la
fila, vuelve a 28/28. Dedup probado inyectando duplicados de `customer_id`
en `bronze_fixture` (uno con `updated_at` viejo, otro con `updated_at`
nuevo y formato sucio): silver queda con 1 fila por clave, la de mayor
`updated_at`, normalizada.

## Estado de validación

- Contra fixtures (`bronze_fixture`): `debug` ✓, `run` 7/7 ✓, `test` 28/28 ✓.
- Contra `bronze` real (100/50/200/1309 filas al validar): `run` 7/7 ✓,
  `test` 28/28 ✓, controles OK, 0 filas de bronze fuera de silver.
