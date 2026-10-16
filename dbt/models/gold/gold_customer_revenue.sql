{{ config(database="lake", schema="gold") }}

-- Gold: facturación acumulada por cliente, solo desde silver
-- (payments -> leases para llegar al cliente). Clave: customer_id.
select
    l.customer_id as customer_id,
    sum(p.amount) as total_paid,
    count(*) as n_payments,
    min(p.paid_at) as first_paid_at,
    max(p.paid_at) as last_paid_at
from {{ ref('silver_payments') }} as p
join {{ ref('silver_leases') }} as l
    on p.lease_id = l.lease_id
group by 1
