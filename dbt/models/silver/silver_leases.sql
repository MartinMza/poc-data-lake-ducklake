{{ config(database="lake", schema="silver") }}

-- Silver de contratos: tipado, limpieza y dedup por lease_id.
-- Normaliza status a minúsculas y conserva solo estados conocidos y
-- rangos de fechas coherentes (end_date nulo = contrato vigente).
with typed as (
    select
        cast(lease_id as bigint) as lease_id,
        cast(customer_id as bigint) as customer_id,
        cast(unit_id as bigint) as unit_id,
        cast(start_date as date) as start_date,
        cast(end_date as date) as end_date,
        lower(trim(cast(status as varchar))) as status,
        cast(created_at as timestamptz) as created_at,
        cast(updated_at as timestamptz) as updated_at
    from {{ source('bronze', 'leases') }}
    where lease_id is not null
),
ranked as (
    select
        *,
        row_number() over (
            partition by lease_id
            order by updated_at desc, lease_id desc
        ) as _rn
    from typed
)
select
    lease_id,
    customer_id,
    unit_id,
    start_date,
    end_date,
    status,
    created_at,
    updated_at
from ranked
where _rn = 1
    and customer_id is not null
    and unit_id is not null
    and start_date is not null
    and status in ('active', 'closed', 'cancelled')
    and (end_date is null or end_date >= start_date)
