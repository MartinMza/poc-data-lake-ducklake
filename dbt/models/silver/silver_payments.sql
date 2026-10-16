{{ config(database="lake", schema="silver") }}

-- Silver de pagos: tipado, limpieza y dedup por payment_id.
-- Conserva solo importes positivos y monedas conocidas.
with typed as (
    select
        cast(payment_id as bigint) as payment_id,
        cast(lease_id as bigint) as lease_id,
        cast(amount as decimal(12, 2)) as amount,
        upper(trim(cast(currency as varchar))) as currency,
        cast(paid_at as timestamptz) as paid_at,
        cast(created_at as timestamptz) as created_at,
        cast(updated_at as timestamptz) as updated_at
    from {{ source('bronze', 'payments') }}
    where payment_id is not null
),
ranked as (
    select
        *,
        row_number() over (
            partition by payment_id
            order by updated_at desc, payment_id desc
        ) as _rn
    from typed
)
select
    payment_id,
    lease_id,
    amount,
    currency,
    paid_at,
    created_at,
    updated_at
from ranked
where _rn = 1
    and lease_id is not null
    and amount is not null
    and amount > 0
    and currency in ('UYU', 'USD')
