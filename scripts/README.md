# lakehouse-infra — procedimiento (tareas 2.1–2.3)

Puertos host (sin colisión con un Postgres local en 5432):

| servicio         | host→contenedor | qué es                              |
|------------------|-----------------|-------------------------------------|
| postgres-catalog | 5433→5432       | catálogo DuckLake                    |
| garage S3        | 3900→3900       | data path DuckLake (Parquet)         |
| garage RPC/web/admin | 3901/3902/3903 | interno/estático/admin              |
| prefect-server   | 4200→4200       | UI/API Prefect                       |
| mssql            | 1433→1433       | origen real de los datos             |
| lake-api         | 8000→8000       | API REST + MCP                       |

> El origen es SQL Server. El servicio `postgres-source` (origen sintético de un
> workstream anterior) ya no existe: si aparece un contenedor con ese nombre,
> es un huérfano y se va con `docker compose up -d --remove-orphans`.

## Puesta en marcha desde cero

```bash
cp .env.example .env   # opcional
docker compose up -d
./scripts/bootstrap.sh
pip install -r scripts/requirements-smoke.txt
python3 scripts/smoke_ducklake.py
```

`bootstrap.sh` es idempotente: re-ejecutarlo no crea buckets, keys ni
databases duplicados (verifica antes de crear y el `bucket allow` se
re-aplica sin efecto). El smoke dropea su tabla al terminar.

## Versiones fijadas (y por qué)

| componente | pin | motivo |
|------------|-----|--------|
| postgres | `postgres:17.6-bookworm` | línea 17 madura (imagen oficial `17.6`+distro); 18.x descartado por cambios breaking (PGDATA version-specific) innecesarios para el PoC |
| garage | `dxflrs/garage:v2.4.1` | estable citada en el quick-start oficial; `v2.3.0+` aporta `--single-node --default-bucket` (auto-provisión idempotente del bucket+key en nodo único) |
| prefect | `prefecthq/prefect:3.6.22-python3.12` | serie 3.6 estable; variante python3.12 alineada con el host |
| duckdb | `duckdb==1.4.4` | rama LTS (1.4.x), más estable que 1.5.x reciente para el PoC |

Actualizar: cambiar el tag en `docker-compose.yml` (o el pin en
`requirements-smoke.txt`), `docker compose pull`, `up -d`, re-correr
bootstrap + smoke. Sin lockfiles adicionales en este slice: las imágenes
son el único artefacto versionado del stack.

## Notas de diseño

- Garage en nodo único con `replication_factor = 1` (ver `docker/garage.toml`);
  fallback documentado en el design: SeaweedFS con la misma interfaz S3.
- Las credenciales S3 del PoC son fijas y solo-locales (auto-provisión
  `GARAGE_DEFAULT_*`); nunca usarlas fuera del PoC.
- Cómo funciona todo, en versión corta: `docs/MANUAL.md`.
- Rollback total: `docker compose down -v` (borra volúmenes y el catálogo).
