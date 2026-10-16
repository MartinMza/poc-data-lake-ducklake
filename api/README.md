# Dulse Lake — API REST (FastAPI) & Servidor MCP (FastMCP)

Capa de servicio unificada para consultar el Data Lake DuckLake (`bronze`, `silver`, `gold` y Time Travel) tanto desde aplicaciones web y dashboards (HTTP REST) como desde agentes de IA (Model Context Protocol).

---

## 1. Inicio Rápido (Un solo comando)

Puedes iniciar todo con el script orquestador:

```bash
# Modo Host (recomendado para desarrollo, con auto-recarga en vivo):
./scripts/run_api.sh

# Modo Docker Compose (levantando el contenedor lake-api):
./scripts/run_api.sh --docker
```

URLs disponibles una vez iniciado:
- **Swagger UI (Documentación interactiva):** [http://localhost:8000/docs](http://localhost:8000/docs)
- **Healthcheck:** [http://localhost:8000/health](http://localhost:8000/health)
- **FastMCP SSE Endpoint:** `http://localhost:8000/mcp/sse`

---

## 2. Configuración Manual en el Host

Si prefieres gestionar el entorno manualmente:

```bash
# 1. Crear entorno virtual con Python 3.12 vía uv
uv venv api/.venv --python 3.12

# 2. Instalar dependencias fijadas
uv pip install -p api/.venv/bin/python -r api/requirements.txt

# 3. Iniciar el servidor FastAPI con Uvicorn
api/.venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 3. Endpoints REST de la API

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/health` | Healthcheck y estado del montaje MCP. |
| `GET` | `/api/v1/tables` | Lista todas las tablas con sus conteos de filas actuales. Filtro `?schemas=bronze,silver,gold`. |
| `GET` | `/api/v1/tables/{schema}/{table}` | Estructura de columnas, tipos de datos, total de filas y muestra de 5 registros. |
| `GET` | `/api/v1/kpis/monthly-revenue` | Ingresos mensuales históricos agregados por moneda (opcional `?currency=USD`). |
| `GET` | `/api/v1/kpis/customer-revenue` | Top clientes ordenados por recaudación total acumulada (`?limit=20`). |
| `GET` | `/api/v1/kpis/unit-performance` | Desempeño y cobranza por unidad habitacional (`?limit=50`). |
| `GET` | `/api/v1/snapshots` | Historial de versiones del catálogo DuckLake para auditoría y Time Travel. |
| `POST` | `/api/v1/query` | Ejecuta consultas analíticas SQL libres con guardrails (`{"sql": "...", "limit": 100}`). |

### Ejemplo de Consulta SQL con `curl`:

```bash
curl -X POST "http://localhost:8000/api/v1/query" \
  -H "Content-Type: application/json" \
  -d '{
    "sql": "SELECT month, currency, total_amount FROM lake.gold.gold_monthly_revenue ORDER BY total_amount DESC LIMIT 3",
    "limit": 10
  }'
```

---

## 4. Conectar Agentes de IA vía MCP (FastMCP)

El servidor MCP comparte el mismo motor DuckDB y expone herramientas (`tools`) y recursos (`resources`) para que los LLMs analicen los datos autónomamente.

### Herramientas MCP expuestas:
1. `list_tables(schemas)`: Lista esquemas y tablas disponibles con conteos.
2. `describe_table(schema, table)`: Inspecciona columnas, tipos y muestra de filas.
3. `get_kpi(metric, limit)`: Acceso directo a métricas curadas (`monthly_revenue`, `customer_revenue`, `unit_performance`).
4. `run_query(sql, limit)`: Ejecuta SQL analítico libre (soporta Time Travel `AT (VERSION => N)`).
5. `list_snapshots(limit)`: Lista snapshots históricos del catálogo.
6. Resource `lake://catalog`: Inventario completo en formato JSON.

### Conexión en Claude Desktop (`claude_desktop_config.json`):

Agrega el servidor bajo la sección `mcpServers`:

```json
{
  "mcpServers": {
    "dulse-datalake": {
      "command": "/home/mmediza/orca/workspaces/POC-Data-Lake/dulse/api/.venv/bin/python",
      "args": ["-m", "api.mcp_server"],
      "env": {
        "CATALOG_HOST": "localhost",
        "CATALOG_PORT": "5433",
        "GARAGE_S3_ENDPOINT": "http://localhost:3900"
      }
    }
  }
}
```

### Conexión vía HTTP / SSE (Cursor, Antigravity u otros agentes remotos):

Con el servidor web activo (`./scripts/run_api.sh`), configura el cliente apuntando a:
```
http://localhost:8000/mcp/sse
```

---

## 5. Guardrails de Seguridad para SQL Libre

Para proteger el lago de modificaciones no autorizadas o saturación de memoria:
- **Solo Lectura Estricto:** La consulta debe comenzar con `SELECT`, `WITH`, `DESCRIBE`, `SUMMARIZE` o `EXPLAIN`. Se rechazan sentencias destructivas (`DROP`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, `TRUNCATE`, `CREATE`, etc.) devolviendo error `400 Bad Request`.
- **Anti Inyección Múltiple:** Se rechaza cualquier consulta que contenga punto y coma (`;`).
- **Row Capping Automático:** Toda consulta se limita automáticamente a un máximo de 1000 filas (default 100) para proteger el contexto del LLM y evitar OOM.

---

## 6. Ejecución de Pruebas

Para validar toda la suite de integración de la API y el servidor MCP contra el Data Lake:

```bash
api/.venv/bin/pytest -v api/test_api_mcp.py
```
