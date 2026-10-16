{{ config(database="lake", schema="gold") }}

-- Gold: facturación por mes y moneda, solo desde silver_payments.
-- Clave: (month, currency).
select
    date_trunc('month', paid_at) as month,
    currency,
    sum(amount) as total_amount,
    count(*) as n_payments
from {{ ref('silver_payments') }}
group by 1, 2
