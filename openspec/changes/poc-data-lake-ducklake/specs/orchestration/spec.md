## ADDED Requirements

### Requirement: Flow punta a punta ordenado
El sistema SHALL proveer un flow de Prefect que ejecute en orden ingesta (`dlt`) → `dbt run` → `dbt test`, deteniendo la secuencia ante el primer fallo y exponiendo un único entrypoint documentado.

#### Scenario: Ejecución exitosa ordenada
- **WHEN** el operador dispara el flow con el stack y datos listos
- **THEN** las tres etapas corren en orden, cada una solo si la anterior tuvo éxito, y el flow termina en estado Completed

#### Scenario: Fallo detiene la cadena
- **WHEN** la ingesta falla de forma determinista (ej. origen caído en entorno de prueba)
- **THEN** `dbt run` y `dbt test` no se ejecutan y el flow queda en estado Failed con el error de la etapa origen

### Requirement: Reintentos, logs y configuración
Cada tarea del flow SHALL tener reintentos configurados y logs centralizados visibles en Prefect, y el flow SHALL parametrizarse por entorno vía variables de entorno documentadas (hosts, credenciales de prueba, escala).

#### Scenario: Reintento y observabilidad
- **WHEN** una tarea falla de forma transitoria y luego se recupera
- **THEN** Prefect muestra los intentos, los logs de cada intento y el éxito final sin intervención manual más allá del reintento automático

#### Scenario: Despliegue contra prefect-server local
- **WHEN** el flow se registra/despliega en el `prefect-server` del compose
- **THEN** es visible en la UI/API del servidor y ejecutable por nombre documentado
