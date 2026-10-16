{{ config(database="lake", schema="silver") }}

-- Silver de unidades: tipado, limpieza y dedup por unit_id.
-- Se descartan filas sin clave y con renta no positiva.
with typed as (
    select
        cast(unit_id as bigint) as unit_id,
        trim(cast(code as varchar)) as code,
        nullif(trim(cast(city as varchar)), '') as city,
        cast(monthly_rent as decimal(12, 2)) as monthly_rent,
        cast(created_at as timestamptz) as created_at,
        cast(updated_at as timestamptz) as updated_at
    from {{ source('bronze', 'units') }}
    where unit_id is not null
),
ranked as (
    select
        *,
        row_number() over (
            partition by unit_id
            order by updated_at desc, unit_id desc
        ) as _rn
    from typed
)
select
    unit_id,
    code,
    city,
    monthly_rent,
    created_at,
    updated_at
from ranked
where _rn = 1
    and monthly_rent is not null
    and monthly_rent > 0
