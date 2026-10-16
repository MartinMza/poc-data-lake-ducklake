## ADDED Requirements

### Requirement: Servicio MSSQL en el compose
El sistema SHALL proveer un servicio `mssql` en `docker-compose.yml` con imagen de SQL Server fijada (tag explícito, sin `latest`), password `SA` inyectada por variable de entorno (nunca hardcodeada) y healthcheck que confirme la instancia aceptando conexiones.

#### Scenario: Levantar con el resto del stack
- **WHEN** el operador ejecuta `docker compose up -d` con `MSSQL_SA_PASSWORD` definida
- **THEN** el servicio `mssql` queda healthy y acepta login `sa` con esa password

#### Scenario: Sin password no arranca con default inseguro
- **WHEN** `MSSQL_SA_PASSWORD` no está definida
- **THEN** el servicio falla de forma explícita en lugar de usar una credencial por defecto

### Requirement: Volumen para el .bak fuera del repo
El servicio SHALL montar un volumen/host-path documentado (ej. `./mssql-backup/` ignorado por git) donde el operador coloca el `.bak` diario, sin commitear el backup al repo.

#### Scenario: Backup visible dentro del contenedor
- **WHEN** el operador coloca el `.bak` en el path documentado y levanta el servicio
- **THEN** el archivo es visible y legible desde dentro del contenedor en la ruta documentada
