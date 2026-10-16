-- ============================================================================
-- PoC Data Lake (DuckLake) — esquema de origen simulado (`postgres-source`)
-- OpenSpec: openspec/changes/poc-data-lake-ducklake, tasks 3.1 / spec source-dataset
--
-- Simula el origen Aurora: 4 tablas relacionadas con PK/FK y columna de
-- incrementalidad `updated_at` (cursor para la ingesta bronze con dlt).
--
-- Aplicar sobre una base vacía:
--   psql "$SOURCE_DSN" -f source/ddl.sql
-- Idempotente: usa CREATE TABLE / CREATE INDEX IF NOT EXISTS.
-- ============================================================================

-- --------------------------------------------------------------------------
-- 1. customers (clientes)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    customer_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    full_name   TEXT        NOT NULL,
    email       TEXT        NOT NULL UNIQUE,
    country     TEXT        NOT NULL DEFAULT 'UY',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- 2. units (unidades / inmuebles arrendables)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS units (
    unit_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code         TEXT         NOT NULL UNIQUE,   -- código interno, ej. 'U-000123'
    city         TEXT         NOT NULL,
    monthly_rent NUMERIC(12,2) NOT NULL CHECK (monthly_rent > 0),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- 3. leases (contratos: N clientes x M unidades en el tiempo)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leases (
    lease_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id BIGINT      NOT NULL REFERENCES customers (customer_id)
                      ON UPDATE CASCADE ON DELETE RESTRICT,
    unit_id     BIGINT      NOT NULL REFERENCES units (unit_id)
                      ON UPDATE CASCADE ON DELETE RESTRICT,
    start_date  DATE        NOT NULL,
    end_date    DATE,  -- NULL = contrato vigente
    status      TEXT        NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'closed', 'cancelled')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (end_date IS NULL OR end_date >= start_date)
);

-- --------------------------------------------------------------------------
-- 4. payments (pagos de un contrato)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payments (
    payment_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lease_id   BIGINT         NOT NULL REFERENCES leases (lease_id)
                      ON UPDATE CASCADE ON DELETE RESTRICT,
    amount     NUMERIC(12,2)  NOT NULL CHECK (amount > 0),
    currency   TEXT           NOT NULL DEFAULT 'UYU' CHECK (currency IN ('UYU', 'USD')),
    paid_at    TIMESTAMPTZ    NOT NULL,
    created_at TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ    NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------
-- Índices para el cursor incremental (`WHERE updated_at > :cursor`) y joins
-- --------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_customers_updated_at ON customers (updated_at);
CREATE INDEX IF NOT EXISTS ix_units_updated_at     ON units (updated_at);
CREATE INDEX IF NOT EXISTS ix_leases_updated_at    ON leases (updated_at);
CREATE INDEX IF NOT EXISTS ix_payments_updated_at  ON payments (updated_at);

CREATE INDEX IF NOT EXISTS ix_leases_customer_id ON leases (customer_id);
CREATE INDEX IF NOT EXISTS ix_leases_unit_id     ON leases (unit_id);
CREATE INDEX IF NOT EXISTS ix_payments_lease_id  ON payments (lease_id);
CREATE INDEX IF NOT EXISTS ix_payments_paid_at   ON payments (paid_at);

-- --------------------------------------------------------------------------
-- Nota de incrementalidad (ver spec source-dataset):
-- `updated_at` la mantiene el escritor (generador sintético / tanda de
-- updates). No hay trigger ON UPDATE a propósito: el cursor incremental de
-- dlt requiere que cada cambio replicable fije `updated_at` explícitamente,
-- y el generador (`source/generate.py --mode update`) lo demuestra.
-- --------------------------------------------------------------------------
