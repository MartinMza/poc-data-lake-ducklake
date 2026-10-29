# Hallazgos — fuente MSSQL real (`mssql-bak-source` 3.2)

> **Nota de vigencia (2026-10-26)**: este documento registra los hallazgos
> históricos del PoC. Después de esas mediciones se retiró el
> origen Postgres sintético y las capas dbt (`silver`/`gold`), y se agregó
> autenticación a la API/MCP. El lago hoy contiene solo las 7 tablas reales
> ingestadas desde SQL Server. Lo que sigue se conserva como evidencia de las
> corridas originales.

Fecha: 2026-10-15. Backup real del operador
`FAF12_Kavos_2026_10_07_233301_411720.bak` (7.5 GB), ERP Kavos/FAF
(agro, collation `Modern_Spanish_CI_AS`). No se mezcla con
`docs/findings.md` (PoC original con datos sintéticos).
Detalle operativo en `bronze/README.md` (§ .bak REAL).

## Versión de imagen — OK sin subir

- Origen: SQL Server 2019 (`SoftwareVersionMajor=15`, build 15.0.4322).
- Imagen fijada: `mcr.microsoft.com/mssql/server:2022-CU27-ubuntu-22.04`
  (`16.0.4295.3`, major 16 ≥ 15). `RESTORE HEADERONLY` + script
  `scripts/restore_mssql.sh` pasaron el gate de versión sin bloqueo.
- Regla vigente: si un futuro `.bak` trae major >16, el script aborta con
  mensaje explícito y hay que subir la imagen (design decisión 1).

## Fricciones (ninguna bloqueante)

1. **Permisos del `.bak`**: copiado como uid host, el motor (`mssql`)
   no lo leía; `chmod` dentro del contenedor exige `--user root`
   (`Operation not permitted` sin él). Documentado en el README.
2. **Restore de 7.5 GB**: varios minutos (mdf final 7.6 G); el script no
   muestra progreso (salida bufferada por `tail`), se monitorea con
   `sys.dm_exec_requests` / tamaño del mdf. Disco: ~15 G totales
   (copia + datafiles), 176 G libres restantes — sin presión.
3. **Nombres CamelCase**: dlt snake_casea (`BSProducto` →
   `bronze.mssql_bs_producto`); `pipeline_mssql.py` ahora normaliza igual
   para los conteos post-carga (las tablas legacy minúsculas no cambian).
4. **Warning dlt `MONEY ... passed to the normalizer`**: benigno;
   sumas verificadas al centavo en 3 tablas (ver README).
5. **Segunda corrida merge**: re-escribe con nuevo `_dlt_load_id`
   (`distinct`=1, conteos idénticos, 0 dupes) — el `load_id` no sirve como
   detector de cambios en tablas `merge` sin cursor; el conteo + sumas sí.

## Decisión incremental vs refresh (por qué no hay incremental real)

- 779 tablas: 169 sin PK, **0 `rowversion`**, 540 sin ninguna columna
  datetime, 14 con nombres cursor-like. El mejor candidato
  (`BSTransaccion.FechaUltimaModificacion`) tiene 15 % de NULLs →
  un incremental perdería ~49k filas desde la carga inicial.
- Decisión: **full-extract diario + `merge`/upsert por PK** (5 tablas
  núcleo) y **`replace`** donde no hay PK (2 marts `DMart_*`).
  El `.bak` es snapshot completo, así que ambas convergen sin duplicados
  (verificado: 2 corridas, 485 557 filas, 0 dupes).
- Tipos fuera del mapeo y excluidos del subset: `IMAGE` (3 tablas de
  documentos/diagramas), `TEXT/NTEXT` en ~20 tablas no ingeridas.
  Ingerirlas exige ampliar `SUPPORTED_TYPES` caso por caso (el pipeline
  aborta con mensaje claro, no trunca).

## Recomendación

El path `.bak` → MSSQL 2022 → `bronze` queda validado con datos reales
(conteos = origen, tipos sin pérdida, re-corridas sin duplicados,
cero impacto en tablas existentes). Para producción: (a) programar
restore + pipeline diarios (hoy manuales); (b) si alguna tabla crece a
millones de filas/día, pedir al ERP una columna de auditoría mantenida
(`updated_at` NOT NULL o `rowversion`) para volver a incremental de
verdad — con el esquema actual no es posible; (c) escalar el subset de
7 → N tablas por prioridad analítica (`DMart_*` + maestros primero,
transaccionales grandes después, documentos `IMAGE` al final o nunca).
