## ADDED Requirements

### Requirement: Restore idempotente del .bak diario
El sistema SHALL proveer un script de restore que cargue el `.bak` en la instancia MSSQL (con `MOVE` de data/log a los paths del contenedor) y que pueda re-ejecutarse sin error sobre una base ya restaurada.

#### Scenario: Restore inicial
- **WHEN** el operador ejecuta el script con el `.bak` presente y la instancia levantada
- **THEN** la base queda restaurada, ONLINE y con las tablas del backup consultables

#### Scenario: Re-ejecución sin duplicados ni error
- **WHEN** el operador ejecuta el script por segunda vez con el mismo `.bak`
- **THEN** termina con éxito y la base converge al mismo estado (sin bases duplicadas)

### Requirement: Verificación post-restore y compatibilidad de versión
El script SHALL verificar tras el restore que la base está ONLINE y listar las tablas restauradas, y SHALL fallar con mensaje explícito si el `.bak` requiere una versión de SQL Server mayor a la de la imagen fijada.

#### Scenario: Backup incompatible
- **WHEN** el `.bak` fue generado por una versión mayor de SQL Server
- **THEN** el script aborta con un error que indica el mismatch de versión y la acción (subir la imagen)
