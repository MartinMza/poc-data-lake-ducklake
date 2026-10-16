# bak-watch-restore Specification

## Purpose
Detección de .bak nuevo y restore condicional dentro del flow Prefect.

## Requirements

### Requirement: Detección de .bak nuevo con marca de restore
El flow SHALL comparar el `.bak` indicado por env (`MSSQL_BAK_PATH`) contra una marca persistida (path + mtime/tamaño del último restore exitoso) y SHALL ejecutar el restore solo si el archivo es nuevo o cambió.

#### Scenario: Sin .bak nuevo se salta
- **WHEN** el `.bak` coincide con la marca del último restore
- **THEN** la tarea termina en éxito sin tocar la instancia y lo indica en el log

#### Scenario: .bak nuevo dispara restore
- **WHEN** aparece un `.bak` distinto (nombre, tamaño o mtime)
- **THEN** se copia a `./mssql-backup/`, se restaura y se actualiza la marca solo si el restore tuvo éxito

### Requirement: Sin variable configurada no falla
Si `MSSQL_BAK_PATH` no está definida, la tarea SHALL terminar en éxito con warning visible, sin bloquear el resto del flow.

#### Scenario: Operador solo-postgres
- **WHEN** el flow corre sin `MSSQL_BAK_PATH`
- **THEN** el restore se salta con warning y la ingesta MSSQL usa la DB ya restaurada
