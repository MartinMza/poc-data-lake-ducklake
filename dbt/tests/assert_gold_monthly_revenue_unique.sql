-- Unicidad de la clave compuesta (month, currency) de gold_monthly_revenue.
-- Test singular: falla si alguna combinación mes/moneda aparece duplicada.
select month, currency, count(*) as n
from {{ ref('gold_monthly_revenue') }}
group by 1, 2
having count(*) > 1
