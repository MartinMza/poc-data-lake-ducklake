{# Sin esta macro el esquema del modelo sale `<profile_schema>_<custom>`
   (observado `main_silver`; ver specs/compat-matrix.md §1.2 caveat 1).
   Con ella los esquemas son exactamente `silver` / `gold`. #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name if custom_schema_name else target.schema }}
{%- endmacro %}
