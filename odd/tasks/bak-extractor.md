# Feature: extractor efímero `.bak` → Parquet

Estado: en diseño. Todavía sin código.

## Specs

### S1 — Aprovisionamiento y control de recursos del motor efímero

Criterios de aceptación, textuales del pedido:

> 1. El entorno de SQL Server debe tener un límite físico de contenedor que no supere los 1.9 GB de RAM y un límite de memoria de proceso (`MSSQL_MEMORY_LIMIT_MB=1800`).
> 2. Antes de iniciar cualquier operación pesada, el sistema debe asegurar que la memoria caché máxima interna del motor (`max server memory`) esté restringida (<= 1200 MB) para evitar que el Buffer Pool retenga páginas innecesarias.
> 3. El ciclo de vida del motor debe ser efímero o dejar el entorno completamente limpio de bases de datos residuales al terminar.

Decisión de ingeniería (documentada, no silenciosa): los tres números de la spec
no son alcanzables a la vez con el techo de ~2 GB del punto 1 del problema
(1.9 GB de contenedor + 250 MB de Python = 2.15 GB; y `MSSQL_MEMORY_LIMIT_MB` no
incluye el overhead de SQLOS sobre el buffer pool). Se usan valores más ajustados
que respetan igual el techo: cgroup **1600 MB**, `MSSQL_MEMORY_LIMIT_MB=1300`,
`max server memory=**1024** MB`, Python con `RLIMIT_AS` **300 MB**.

### S2 — Descubrimiento y restauración autónoma del `.bak`

> 1. Dado un archivo `.bak`, el sistema debe inspeccionar automáticamente sus metadatos previos a la restauración (`RESTORE FILELISTONLY`) para descubrir los nombres lógicos de todos sus archivos de datos (`.mdf`/`.ndf`) y de log (`.ldf`).
> 2. El sistema debe ejecutar la restauración de la base de datos reubicando dinámicamente (`WITH MOVE`) esos archivos lógicos a las rutas válidas dentro del contenedor temporal.
> 3. Si la restauración falla (por archivo corrupto, versión incompatible o falta de espacio), el sistema debe abortar inmediatamente informando el motivo exacto del motor SQL.

### S3 — Descubrimiento dinámico del esquema y catálogo de tablas

> 1. El sistema debe consultar el catálogo de la base restaurada y listar únicamente las tablas base de usuario (`BASE TABLE`), excluyendo vistas y tablas de sistema.
> 2. Debe preservar la identificación tanto del esquema (`SCHEMA`) como del nombre de la tabla (`TABLE_NAME`).
> 3. Debe permitir exportar tablas vacías generando un archivo Parquet válido que conserve la definición del esquema de columnas aunque tenga 0 filas.

### S4 — Extracción en streaming columnar (zero-copy) a Parquet

> 1. La lectura de cada tabla no debe bloquear el motor ni reservar memoria de transacciones innecesaria durante la lectura (`WITH (NOLOCK)`).
> 2. Los datos deben fluir en lotes configurables (ej. 50.000 filas por batch) desde el driver ODBC directamente hacia estructuras de Apache Arrow y escribirse incrementalmente en disco en formato Parquet.
> 3. Los archivos Parquet resultantes deben usar compresión `ZSTD` y preservar fielmente los tipos de datos nativos de origen (numéricos exactos/decimales, fechas con precisión, booleanos, textos, nulos) sin degradarlos todos a texto.
> 4. El sistema debe manejar de forma segura columnas de texto o binarios de longitud máxima (`VARCHAR(MAX)`, `NVARCHAR(MAX)`, `VARBINARY(MAX)`) acotando los buffers de pre-asignación (`max_text_size` / `max_binary_size`) para evitar picos de memoria en el lector ODBC.

Restricciones no negociables asociadas (textuales):

> - **Transferencia de datos:** Estrictamente en lotes mediante `arrow-odbc` y `pyarrow`. Queda prohibido el uso de `pandas`, `sqlalchemy` ORM o cargar tablas completas en memoria.

Huella objetivo (textual):

> Para mantener una huella de memoria RAM plana y constante en Python (< 250 MB) sin importar si una tabla tiene 1.000 o 100 millones de filas.

### S5 — Higiene de memoria entre tablas y limpieza final

> 1. Al finalizar la exportación de cada tabla, el sistema debe instruir al motor SQL Server para que purgue las páginas limpias de su Buffer Pool (`DBCC DROPCLEANBUFFERS`), garantizando que la lectura de la siguiente tabla comience con la memoria liberada.
> 2. Una vez exportadas todas las tablas (o ante un fallo irrecuperable mediante un bloque `finally`), el sistema debe eliminar la base de datos temporal restaurada para liberar el espacio en disco de los archivos `.mdf` y `.ldf`.
> 3. El proceso debe entregar un resumen final con: cantidad de tablas detectadas, tablas exportadas con éxito, filas procesadas, tiempo por tabla y cualquier tabla que haya arrojado error.

### S6 — Entregables y forma del proyecto

> 1. **Propuesta de arquitectura de archivos** del proyecto antes de escribir el código completo.
> 2. **Definición de infraestructura (`docker-compose.yml` y `Dockerfile`)** que contemple el servicio de SQL Server limitado a 1.9 GB de RAM y el contenedor/entorno de Python con `ODBC Driver 18 for SQL Server` preinstalado.
> 3. **Código fuente modular en Python** que implemente el Camino a la Solución y cumpla con las 5 Specs, configurable por variables de entorno o argumentos CLI (ruta del `.bak`, directorio de salida Parquet, tamaño de lote y límites de buffer para campos `MAX`).

## Contrato de salida (congelado: lo consumen dos unidades en paralelo)

- Un archivo por tabla: `<out>/<schema>/<table>.parquet`.
- Compresión ZSTD, un solo row-group por batch (sin reescribir el archivo).
- CLI: `python -m bak2parquet --bak <ruta> --out <dir> [--batch-size N]
  [--max-text-size N] [--max-binary-size N] [--report-json <ruta>]`.
- Exit 0 solo si todas las tablas se exportaron; exit != 0 con el motivo del
  motor en stderr si falla restore, conexión o una tabla.
- Nombres de archivo saneados (sin `/`, `\\`, `:` ni bytes nulos).
- Motor en dos modos: `--engine docker` (efímero, arranca su propio contenedor
  con los límites de S1) y `--engine url` (se conecta a una instancia existente,
  para compose sin Docker-in-Docker). En modo `url` el teardown NO detiene el
  motor: solo `DROP DATABASE` de la base que esa corrida creó.
- `pyodbc` solo para control (`RESTORE`, `DBCC`, catálogo); `arrow-odbc`
  exclusivamente para el flujo de datos. `pyodbc` nunca transfiere filas de
  usuario.

## Tasks

Ruta `A` = paquete nuevo. Ruta `B` = retiro del camino dlt/bcp.

- **T1** (S6.1) arquitectura y contrato de salida — hecho
- **T2** (A, S6.3) config validada, CLI y reporte
- **T3** (A, S1) motor efímero: arranque, `max server memory`, readiness, teardown
- **T4** (A, S2) `FILELISTONLY` + `WITH MOVE` + aborto con el motivo del motor
- **T5** (A, S3.1-S3.2) catálogo de tablas base de usuario por schema
- **T6** (A, S3.3, S4) extracción arrow-odbc → Parquet ZSTD, `MAX` acotado, 0 filas
- **T7** (A, S5) `DROPCLEANBUFFERS` por tabla, `DROP DATABASE` con guarda, resumen
- **T8** (A, S1.1, S6.2) `Dockerfile` con ODBC 18 y compose con cgroup/cpus
- **T9** (A) tests unitarios + integración con `test-source.bak` (528 KB)
- **T10** (B) cargador Parquet → DuckLake Bronze (requerido por el alcance B,
  no figura en las specs del usuario)
- **T11** (B) retirar `fast_mssql.py`, `pipeline_mssql.py` y las deps dlt/sqlalchemy
- **T12** (B) `scripts/container_init.sh` delega en extractor + cargador
- **T13** (B) `orchestration/flow.py`: tarea bak → parquet → bronze
- **T14** verificación bajo el techo de memoria y corrida real con el `.bak` de 7 GB
- **T15** documentación (README, `docs/MANUAL.md`, `bronze/README.md`)

## Log

- **L1** (pedido original, textual): "Necesito que me mejores el sistema actual para que consuma menos recursos." seguido de la especificación completa del *Extractor Efímero de `.bak` (SQL Server) a Data Lake (Parquet)*, reproducida en `## Specs` S1-S6 (texto del usuario, sin resumir).
- **L2** (decisión del usuario, textual): "B · Reemplaza la extracción de Bronze". Se retiran `bronze/fast_mssql.py` (bcp) y `bronze/pipeline_mssql.py` (dlt); Bronze pasa a leer Parquet y `orchestration/flow.py` se re-cablea.
- **L3** (hallazgo del orquestador): `fast_mssql.py` no solo extrae, también escribe en DuckLake con una allowlist curada (`MSSQL_TABLES="BSMoneda:MonedaID:merge,..."`). La spec termina en Parquet, así que B exige un cargador Parquet → DuckLake (T10) para no dejar el lago sin Bronze. La allowlist y los modos `merge`/`replace` por tabla desaparecen: al extraer siempre el backup completo, cada tabla se reemplaza entera (idempotente).
- **L4** (aceptación sobre el backup real de 7 GB, `FAF12_Kavos_2026_10_07_233301_411720.bak`): **778/778 tablas, 22.697.112 filas, 188 s, exit 0, pico de cgroup 443 MiB, cero bases residuales**. Tipos preservados (`decimal128(5,2)`, `int32`, `timestamp[ms]`, `bool`) y ZSTD en el 100% de los archivos. El camino hasta acá destapó siete causas que ningún test con el `.bak` de 528 KB ni ningún test de strings SQL podía ver:
  1. `RLIMIT_AS` como guardia de memoria: limita espacio de direcciones, mata con RSS de 93 MB (SIGSEGV).
  2. `max_text_size` en 4 MiB: el lector cree que cada fila mide 4,2 MB, los lotes caen a ~11 filas y el RSS llega a 1.235 MB.
  3. Un row group por lote: 3.800 grupos en una tabla de 19 M de filas, ~0,5 MB de estadísticas cada uno → +1,9 GB.
  4. jemalloc retiene páginas liberadas: el piso crece ~0,3 MB por tabla con `total_allocated_bytes()=0`.
  5. `text`/`ntext`/`image` tienen `max_length=16` y no `-1`: quedaban fuera del inventario y de la sonda.
  6. La sonda de columnas consultaba `sys.columns` sin calificar la base sobre la conexión de control (que vive en `master`): siempre devolvía cero.
  7. `PROBE_FLOOR_BYTES=64`: con un LOB vacío el techo global caía a 64 B y truncaba `varchar` de 135 B.
- **L5** (límites medidos que reemplazan a los de diseño): presupuesto del cliente **400 MB** (no 300) con motor en `max server memory=1024 MB` (RSS medido 1.360 MB) → total 1,76 GB, dentro del techo de ~2 GB. Documentado en `config.py`, no silenciado.
