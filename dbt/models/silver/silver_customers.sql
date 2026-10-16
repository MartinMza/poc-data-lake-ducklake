{{ config(database="lake", schema="silver") }}

-- Silver de clientes: tipado, limpieza y dedup por clave de negocio.
-- Una fila por customer_id: la de mayor updated_at (desempate por
-- customer_id para determinismo). Se descartan filas sin clave.
with typed as (
    select
        cast(customer_id as bigint) as customer_id,
        nullif(trim(cast(full_name as varchar)), '') as full_name,
        lower(trim(cast(email as varchar))) as email,
        coalesce(
            nullif(upper(trim(cast(country as varchar))), ''),
            'UY'
        ) as country,
        cast(created_at as timestamptz) as created_at,
        cast(updated_at as timestamptz) as updated_at
    from {{ source('bronze', 'customers') }}
    where customer_id is not null
),
ranked as (
    select
        *,
        row_number() over (
            partition by customer_id
            order by updated_at desc, customer_id desc
        ) as _rn
    from typed
)
select
    customer_id,
    full_name,
    email,
    country,
    created_at,
    updated_at
from ranked
where _rn = 1
