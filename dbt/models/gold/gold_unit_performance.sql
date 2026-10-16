{{ config(database="lake", schema="gold") }}

-- Gold: desempeño por unidad (renta, ocupación y cobro), solo desde silver.
-- Clave: unit_id. Unidades sin contratos/pagos aparecen con ceros.
with lease_pay as (
    select
        l.unit_id as unit_id,
        l.lease_id as lease_id,
        l.status as status,
        sum(p.amount) as collected
    from {{ ref('silver_leases') }} as l
    left join {{ ref('silver_payments') }} as p
        on p.lease_id = l.lease_id
    group by 1, 2, 3
),
per_unit as (
    select
        unit_id,
        count(*) as n_leases,
        count(*) filter (where status = 'active') as n_active_leases,
        coalesce(sum(collected), 0) as total_collected
    from lease_pay
    group by 1
)
select
    u.unit_id as unit_id,
    u.city as city,
    u.monthly_rent as monthly_rent,
    coalesce(pu.n_leases, 0) as n_leases,
    coalesce(pu.n_active_leases, 0) as n_active_leases,
    coalesce(pu.total_collected, 0) as total_collected
from {{ ref('silver_units') }} as u
left join per_unit as pu
    on pu.unit_id = u.unit_id
