# Feature: adquisición automática del `.bak` desde el correo

Estado: en implementación.

Objetivo: que un `.bak.tar.gz` nuevo llegue solo a `mssql-backup/` y dispare la
ingesta a Bronze, sin intervención manual. Hoy el tramo manual es únicamente la
adquisición: `flow.py` ya detecta archivos nuevos por `size+mtime`, soporta
`.tar.gz`, tiene guarda anti-copia-parcial y actualiza su marca solo en éxito.

## Specs

### S1 — Disparador y cadencia

Textual del pedido:

> Necesito automatizar el proceso de obtener el backup que luego se carga en
> mserver. Yo lo recibo atravez de un correo todos los dias una o dos veces por
> semana. Como puedo hacer para automatizar la descarga del archivo y cargarlo
> al lake?

Textual de las respuestas de alcance:

> Un link de descarga

> Gmail

Decisión de ingeniería (documentada, no silenciosa): el pedido dice "todos los
dias" y a la vez "una o dos veces por semana", que son lecturas incompatibles.
Se implementa la **lectura más estricta**: el poller corre con una cadencia
configurable cuyo default es **cada 4 h** (`ACQUIRE_POLL_HOURS=4`), que cubre
"todos los días" y sobra para el caso semanal. La cadencia no es una preferencia
estética: la impone S2 (ventana de 12 h del link).

1. El sistema debe revisar el buzón de Gmail de forma periódica y autónoma,
   sin intervención humana.
2. Un correo que no matchee remitente y asunto configurados no debe producir
   ninguna descarga ni ningún error visible.

Requisito agregado por el usuario (L10), textual: "Muy importante filtrar por
fecha".

3. El poller debe filtrar por fecha en el propio buzón (`IMAP SINCE`), no
   recorrer el historial completo de avisos. Un buzón con dos años de correos
   no debe implicar dos años de `FETCH` en la primera corrida.
4. La ventana de búsqueda debe ser configurable (`ACQUIRE_SINCE_DAYS`) y su
   default tiene que ser menor que la ventana de la firma del link: un correo
   más viejo que la firma no aporta nada descargable.

### S2 — Forma real del artefacto y ventana de validez

Forma verificada del enlace (credenciales redactadas a propósito):

```
https://finnegans-virginia-db-backup-diario.s3.us-east-1.amazonaws.com/
  2026-11-01/FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz
  ?X-Amz-Algorithm=AWS4-HMAC-SHA256
  &X-Amz-Credential=ASIA…%2F20261102%2Fus-east-1%2Fs3%2Faws4_request
  &X-Amz-Date=20261102T090005Z
  &X-Amz-Expires=43200
  &X-Amz-Security-Token=<redactado>
  &X-Amz-Signature=<redactado>
  &x-amz-checksum-mode=ENABLED
  &x-id=GetObject
```

Evidencia de caducidad (comprobada contra el reloj del host):

> ahora (UTC): 2026-11-03T04:18:16Z
> firma del link: 2026-11-02T09:00:05Z
> expira (43200s): 2026-11-02T21:00:05Z
> restante: -7.3 h -> EXPIRADO

1. El artefacto es **S3 presigned**: no requiere credenciales propias, la URL
   *es* la capacidad. No se usa SDK de AWS ni se configuran claves de AWS.
2. El sistema debe rechazar una descarga cuya ventana ya venció, o cuya ventana
   restante sea insuficiente para terminar, con un error accionable ("pedir
   link nuevo"), sin dejar archivos parciales en `mssql-backup/`.
3. Si la ventana vence durante la descarga, el fallo debe distinguirse del ruido
   de red: reintentar el resume es inútil y el sistema no debe reintentar.
4. El sistema debe reanudar una descarga cortada por red (`Range`) mientras la
   ventana siga vigente.

Recomendación registrada para el proveedor (no bloqueante): `X-Amz-Expires`
acepta hasta 604800 s (7 días). Pedir esa ventana a Finnegans elimina la clase
de falla "el link murió en el medio", que es la más probable con 7,5 GB.

### S3 — Nunca retroceder los datos

El nombre del artefacto codifica la fecha:
`FAF12_Kavos_<YYYY>_<MM>_<DD>_<HHMMSS>_<6 dígitos>.bak.tar.gz`.

1. El sistema debe parsear ese timestamp y **rechazar** un backup cuyo
   timestamp sea menor o igual al último aplicado con éxito.
2. Un backup rechazado por antigüedad no debe disparar la ingesta ni
   actualizar el estado como si hubiera sido aplicado.

Motivo: `bronze-load` hace reemplazo total por tabla. Cargar el backup de ayer
por un reintento o un link viejo **retrocede el lago en el tiempo sin ningún
error**. Esta es la guarda que separa automatizar de automatizar mal.

### S4 — Integridad verificada

1. La descarga debe verificarse contra el checksum que S3 devuelve
   (`x-amz-checksum-crc32`, por la marca `x-amz-checksum-mode=ENABLED`),
   calculado en streaming mientras se escribe.
2. El tamaño final debe coincidir con `Content-Length`.
3. Un archivo que no verifica **no** debe llegar a `mssql-backup/`: se queda en
   `.incoming/` como `.part` y se reporta.

### S5 — La URL es un secreto

1. La URL presignada no debe aparecer en logs, en el estado en disco, en
   `argv` de ningún subproceso, ni en mensajes de error.
2. El estado persistido guarda `Message-ID`, UID y timestamps de backup;
   nunca URLs.
3. La descarga ocurre en el proceso del poller (cliente HTTP en proceso), no
   como subproceso: `flow.py::_run` loguea `shlex.join(cmd)` y además `ps`
   expone `argv`.

### S6 — Solo se descarga de destinos conocidos

1. El poller debe extraer links del cuerpo (`text/html` y `text/plain`) y
   aceptar solo hosts de una allowlist explícita, sobre `https`.
2. Un link de un host fuera de la allowlist no se descarga: se reporta.
3. El remitente se compara por dirección exacta, no por sufijo.

### S7 — Integración con la ingesta y periodicidad

1. `orchestration/flow.py` debe incorporar la adquisición como primera tarea,
   antes de `bak-extract`, con los mismos reintentos configurables.
2. Sin `.bak` nuevo, la corrida completa debe terminar en éxito rápido sin
   tocar el motor (comportamiento que `bak-extract` ya garantiza).
3. Debe existir una forma declarada de correr el flow con schedule, sin
   cron externo improvisado.

### S8 — Disco y retención

1. Antes de descargar, el sistema debe verificar espacio libre suficiente y
   abortar con mensaje claro si no.

Requisito agregado por el usuario (L10), textual: "siempre sobre escribir y
dejar un solo archivo backup".

2. Tras una adquisición exitosa, el directorio final debe quedar con
   **exactamente un** artefacto de backup fechado: el nuevo reemplaza al
   anterior. No se acumulan versiones.
3. La poda automática corre **después** de que el artefacto nuevo verificó
   checksum y quedó commiteado, nunca antes: borrar el anterior para hacerle
   lugar al nuevo, con el nuevo sin verificar, sería perder los dos.
4. Lo que no parsea como backup fechado nunca se borra (`test-source.bak`,
   `bak2pq-synth.bak` y cualquier fixture quedan intactos). El filtro es el
   nombre, no el tamaño ni la extensión.
5. La poda tiene que ser una sola implementación reusada por el flow y por el
   CLI: dos rutas de borrado distintas es cómo se pierde un backup.

### S9 — Fallas visibles

1. Un fallo de adquisición no debe ser silencioso: la corrida debe quedar en
   estado fallido en Prefect y dejar el motivo.

## Tasks

| ID | Specs | Qué | Ruta | Commit |
|---|---|---|---|---|
| T1 | S3 | Identidad del backup: parseo del nombre + guarda de monotonicidad | `orchestration/kavos_acquire/identity.py` | `ce4ef90` |
| T2 | S5 | Estado persistido: JSON atómico, Message-IDs, timestamps aplicados | `orchestration/kavos_acquire/state.py` | `4fbbc45` |
| T3 | S2,S4,S8 | Descarga reanudable, checksum, guarda de ventana y de disco, commit atómico | `orchestration/kavos_acquire/download.py` | `fa326ee` |
| T4 | S6,S1 | Poller IMAP de Gmail: fecha (`SINCE`), remitente/asunto, links, allowlist | `orchestration/kavos_acquire/mailbox.py` | `d53aed4` |
| T5 | — | Resolver pluggable por host (directo/S3, OneDrive, Drive) | `orchestration/kavos_acquire/resolvers.py` | `dc7a8ba` |
| T6 | S7 | Tarea `acquire` en el flow + forma de correrlo con schedule | `orchestration/flow.py`, `orchestration/serve.py` | `c9233a6`, `ea9c816` |
| T7 | S8 | Retención: exactamente un backup, una sola implementación | `orchestration/kavos_acquire/retention.py`, `scripts/prune_backups.py` | `87a27cd` |
| T8 | S1,S2,S9 | Documentación y variables de entorno | `docs/MANUAL.md`, `README.md`, `.env.example` | `5ff909d` |

### Límite de slice (importante para el review)

La rama `feat/bak-acquire` sale de **`feat/bak2parquet`**, no de `main`.
Contar el diff contra `main` da ~25 000 líneas y ~61 commits, que son casi
todos trabajo previo ajeno a esta feature. El review de esta entrega se mide
contra su base real:

```
git diff --shortstat feat/bak2parquet..feat/bak-acquire
git log --oneline feat/bak2parquet..feat/bak-acquire
```

Los slices de commit son, en orden: spec (`eb383fc`), andamiaje (`1fa9d11`),
identidad (`ce4ef90`), estado (`4fbbc45`), descarga (`fa326ee`), buzón
(`d53aed4`), resolvers (`dc7a8ba`), orquestador (`c9233a6`), flow+schedule
(`ea9c816`), poda (`87a27cd`), docs (`5ff909d`). Cada uno es revisable solo.

### Verificación ejecutada

| Qué | Comando | Resultado |
|---|---|---|
| Suite del componente | `orchestration/.venv/bin/python -m pytest orchestration/tests/` | `98 passed` |
| Flow completo sin mutar el lago | `MSSQL_BAK_PATH= BRONZE_PYTHON=/bin/true orchestration/.venv/bin/python orchestration/flow.py` | `flow kavos-lake-medallion: Completed` (3 tasks, `AcquireOutcome` atraviesa Prefect) |
| Poda sobre el directorio real | `orchestration/.venv/bin/python scripts/prune_backups.py` (dry-run) | conserva el `.bak` real, ignora los 2 fixtures sin fecha |


## Log

### L1 — Pedido original (textual)

> Necesito automatizar el proceso de obtener el backup que luego se carga en
> mserver. Yo lo recibo atravez de un correo todos los dias una o dos veces por
> semana. Como puedo hacer para automatizar la descarga del archivo y cargarlo
> al lake?

### L2 — Alcance confirmado por el usuario (textual)

> Un link de descarga

> Gmail

### L3 — Evidencia: forma real del enlace (credenciales redactadas)

Se recibió un enlace de ejemplo. Se transcribe la estructura con
`X-Amz-Security-Token` y `X-Amz-Signature` redactados, porque son una
credencial STS viva: commitear la URL completa filtraría una capacidad de
lectura sobre el objeto.

```
https://finnegans-virginia-db-backup-diario.s3.us-east-1.amazonaws.com/
  2026-11-01/FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz?X-Amz-Algorithm=AWS4-HMAC-SHA256&…
```

Hechos derivados: bucket `finnegans-virginia-db-backup-diario`, región
`us-east-1`, objeto `.bak.tar.gz`, ventana `X-Amz-Expires=43200` (12 h),
`x-amz-checksum-mode=ENABLED`.

### L4 — Evidencia: el enlace de ejemplo ya estaba vencido

```
ahora (UTC): 2026-11-03T04:18:16Z
firma del link: 2026-11-02T09:00:05Z
expira (43200s): 2026-11-02T21:00:05Z
restante: -7.3 h -> EXPIRADO
```

Consecuencia de diseño: la ventana de 12 h es la restricción dominante y el
poller no puede ser diario a hora fija.

### L5 — Decisión: resolver por host

El caso real es S3 presigned (sin resolución: GET directo). Se implementan
`direct` (S3, OneDrive/SharePoint `&download=1`) y `gdrive` (token `confirm`).
WeTransfer no se implementa como scraper: su descarga depende de un endpoint no
documentado que cambia sin aviso, y shipear eso sin poder probarlo sería peor
que fallar cerrado con un mensaje accionable que pida S3 o SharePoint.

### L6 — Decisión: lectura estricta de la cadencia

Ver S1. "todos los dias" y "una o dos veces por semana" son incompatibles; se
implementa la lectura más estricta (cada 4 h).

### L7 — Verificación independiente: dos hallazgos reales, corregidos

`gentle-ai-verify` sobre `feat/bak2parquet..feat/bak-acquire`. Items 1 y 2
(riesgo alto) PASS. Dos fallas en el detalle, ambas reproducidas y corregidas
en `0825bad` con test de regresión:

1. **El camino 416 publicaba sin verificar.** `esperado = _total_from_headers(...)
   or ya_descargado` hacía que un 416 sin `Content-Range` ni checksum tomara el
   tamaño actual como esperado: la verificación de tamaño quedaba vacua y un
   `.part` de 13 bytes de basura se publicaba en `mssql-backup/`. S3 real manda
   `Content-Range` en un 416; OneDrive y Drive comparten `download()` y no.
   Ahora falla cerrado.
2. **Fuga de la query en los mensajes.** `mailbox._reason_for` interpolaba la
   URL completa cuando no había host, y esa query lleva `X-Amz-Security-Token`.
   El texto llegaba al log de Prefect vía `AcquisitionFailed`. Ahora pasa por
   `sanitize_url`.

Evidencia del estado final: `100 passed` y flow completo en `Completed`.

### L8 — Review nativo bloqueado por assets desactualizados

`gentle_review assess` → `reviewDue: true`, `nativeRisk: high`, candidato
`base-diff` contra `feat/bak2parquet` (28 paths, 3513 líneas), `consumed: false`.

`gentle_review inspect` → `status: blocked`, `reason_code: managed_assets_outdated`.
La continuación impone `gentle-ai sync --agent pi`, que reescribe assets
gestionados en la instalación de Pi (`~/.pi/agent/npm/node_modules/gentle-pi/`),
fuera del repositorio. No se ejecuta sin autorización explícita: es una acción
de mantenimiento sobre el entorno del operador, no sobre el proyecto.

Consecuencia: el candidato **no** fue revisado por el review nativo. Lo que sí
existe como verificación es la suite de 100 tests, el check funcional del flow y
la verificación independiente de `gentle-ai-verify` (L7).

### L9 — Evidencia: el correo real (estructura verificada)

El operador aportó el aviso real completo. Hechos que fijaron el diseño:

- `From: Finnegans Go <info@finnegans.com.ar>` — dominio `.com.ar`.
- `Subject: Backup de Base de Datos Disponible`.
- Envío por Amazon SES en nombre de Finnegans (`Return-Path: …@info.finnegans.com.ar`).
- `multipart/alternative`: el `text/plain` **no** trae el link; sólo el
  `text/html`, con `<a href='…'>` entre comillas **simples** y la URL cortada en
  **8 puntos** por quoted-printable (`=3D` para los `=`).
- Casilla destino `mail.drive.mm@gmail.com`. No se commitea: es un dato del
  operador, va en su `.env`.

**Defecto encontrado y corregido**: los únicos valores de remitente y asunto que
existían (`backup@finnegans.com`, "Backup diario") no matcheaban el aviso real,
así que el poller nunca habría descargado nada — y el fallo habría sido
silencioso (corrida exitosa, cero backups nuevos). Los valores del proveedor
ahora viven en `kavos_acquire/defaults.py`, con override por
`ACQUIRE_FROM`/`ACQUIRE_SUBJECT_PATTERN`, y `tests/test_mailbox.py` los compara
contra el correo real.

Nota: `.env.example` quedó con los valores viejos porque el harness bloquea la
escritura en ese path por ser sensible. Los dos valores correctos están en
`defaults.py`, que es el que manda cuando la variable no está definida; conviene
alinear el ejemplo a mano.

Verificación del parser contra el aviso real (fixture redactado, token y firma
reemplazados): `sender='info@finnegans.com.ar'`, 1 link de 450 caracteres
intacto pese a los 8 cortes.

### L10 — Cambio de requisito del usuario (textual)

> listo. Ya cargue las credenciales. Muy importante filtrar por fecha y siempre
> sobre escribir y dejar un solo archivo backup

Interpretación aplicada, en dos partes:

- **"filtrar por fecha"** → `IMAP SINCE` con ventana configurable
  (`ACQUIRE_SINCE_DAYS`, default 3, menor que la firma de 12 h para no buscar
  correos que ya no se pueden bajar). Reescribe S1, reabre T4.
- **"siempre sobrescribir y dejar un solo archivo"** → retención **1**: tras una
  adquisición exitosa y verificada, el anterior se borra y queda sólo el nuevo.
  Reescribe S8, reabre T7 y agrega la poda automática al orquestador.

La lógica de borrado se extrae a `kavos_acquire/retention.py` y la comparten el
flow y el CLI: la versión anterior la tenía sólo `scripts/prune_backups.py`, y
duplicarla para el uso automático sería tener dos rutas de borrado.

Riesgo asumido explícitamente por el usuario: con retención 1, si la firma del
link nuevo vence antes de bajarlo, el backup anterior ya no está y hay que
pedirlo de nuevo. La poda corre sólo después de verificar checksum, así que
nunca se borra el anterior para hacerle lugar a uno dudoso.

### L11 — Verificación del cambio de requisito (L10)

| Qué | Evidencia |
|---|---|
| Filtro por fecha en el buzón | `scan_mailbox` arma `UID n:* SINCE 1-Nov-2026`; fecha armada a mano, no con `strftime('%b')`, que depende del locale y devolvería "nov" |
| Retención 1, una sola implementación | `kavos_acquire/retention.py` la usan el orquestador y el CLI; `apply_retention` es el único `unlink` del proyecto |
| Sólo se poda después de verificar | `pipeline._podar` corre después de `download()` (checksum + `os.replace`); test `test_si_la_descarga_falla_el_backup_anterior_sigue_ahi` |
| Fixtures protegidos | `test-source.bak`, `bak2pq-synth.bak` y `notas.txt` quedan como "ignorados" y nunca se borran |
| Suite | `115 passed` |
| CLI sobre el directorio real | `keep=1`, conserva el `.bak` del 07/10, ignora los 2 fixtures |
| Flow completo | `Completed` con `acquire` en skip |

Corrección de diseño durante la implementación: `freed_bytes` era una property
que leía el disco, así que el resumen del CLI —impreso **después** de aplicar—
siempre decía "0 bytes liberados". Pasó a capturarse en el momento de
planificar.

### L12 — Servicio `prefect-serve` en el stack

El schedule estaba escrito pero nadie lo levantaba: `ACQUIRE_POLL_HOURS` no tenía
lector. Se agrega el servicio `prefect-serve` a `docker-compose.yml` con
`restart: unless-stopped`, reusando la imagen `kavos-lake-pipeline` (la misma de
`lake-init`), que ya trae `bronze/.venv` con ODBC + arrow-odbc y
`orchestration/.venv` con `prefect==3.8.8` y `httpx`.

Bloqueante preexistente encontrado: `docker/Dockerfile.pipeline` copiaba
`dbt/requirements.txt`, que dejó de existir al retirarse la capa silver/gold. El
build de **todo** el stack fallaba, `lake-init` incluido. Se quitaron esas tres
líneas.

Decisión de estado: `ACQUIRE_STATE_PATH` y `ORCH_RESTORE_MARK` apuntan al
volumen `serve-state`, no al filesystem del contenedor. Si el estado se perdiera
al recrear el contenedor, la guarda de antigüedad olvidaría el último backup
aplicado y podría volver a cargar uno viejo: exactamente el fallo que S3 previene.

Consecuencia operativa: con el servicio corriendo, `flow.py` a mano compite por
el mismo estado; hay que elegir uno.

**Hallazgo de seguridad del proceso**: `docker compose config` renderiza las
variables sin enmascarar, así que la app password de Gmail terminó en la salida
de la sesión. Hay que rotarla. Nunca correr `docker compose config` con la salida
capturada en un log.

### L13 — Verificación end-to-end real, en Docker

Corrida completa dentro del contenedor `prefect-serve`, con un aviso real de
Finnegans y la imagen `kavos-lake-pipeline`.

**Resultado**: flujo `Completed`. Números medidos:

| Métrica | Valor |
|---|---|
| Artefacto adquirido | `FAF12_Kavos_2026_11_03_233302_428734.bak.tar.gz` (1,208 GB) |
| Verificación | CRC32 de S3 sobre el archivo completo |
| Poda | borró el `.bak` del 2026-10-07 (7,5 GB) y luego el derivado (7,66 GB) |
| Tablas extraídas | 788 |
| Filas cargadas en Bronze | 23 225 319 |
| Tiempo de carga | 226,9 s |
| Tiempo total de la corrida | ~8 min (incluye build de imagen) |

`mssql-backup/` final: **1,2 GB**, un solo artefacto fechado más los dos
fixtures ignorados.

**Defectos que sólo aparecieron acá, todos corregidos:**

1. `No module named 'pyodbc'`. La imagen instalaba `bronze/requirements.txt` en
   `bronze/.venv`, que no trae las deps del extractor; el flow corre
   `python -m bak2parquet` con ese intérprete. `container_init.sh` las instalaba
   en runtime, `serve.py` no. Ahora se hornean en el build.
2. **El reintento se perdía para siempre.** La adquisición se consume una sola
   vez (UID y Message-ID avanzan). Si la extracción falla, el artefacto queda en
   disco y ninguna corrida posterior lo mira. `bak_path_for` ahora cae al
   artefacto más nuevo del disco, y la marca de `bak-extract` evita repetir
   trabajo.
3. **Ese fallback salteaba la guarda de antigüedad** (corre sólo en `acquire`),
   así que podía ingerir un backup viejo y retroceder el lago. Ahora compara
   contra el último aplicado.
4. **Dos artefactos del mismo backup.** La poda corre en `acquire`, antes de que
   `bak-extract` descomprima el `.tar.gz`: quedaban 8,9 GB en vez de 1,2 GB.
   Ahora el `.bak` derivado se borra después de que el extractor tiene éxito, y
   nunca si el comprimido ya no está (sería la única copia).

**Error de proceso propio**: la primera corrida falló porque compilé la imagen,
después cambié el código y recreé el contenedor sin recompilar. El contenedor
corría un `flow.py` sin el fallback.

**Hallazgo colateral**: la documentación afirmaba "7 tablas / 485 557 filas" en
diez lugares. El catálogo dinámico no tiene allowlist desde el commit
`009e101`, así que la realidad es 788 tablas / 23 225 319 filas. Se corrigieron
`README.md` y `docs/MANUAL.md` para describir el catálogo completo y consultar
los conteos en vez de citarlos de memoria.

### L14 — Documentación al día y guardas contra la repetición

**Documentación.** Se corrigió la deriva en `README.md`, `docs/MANUAL.md`,
`docs/ARCHITECTURE.md`, `docs/API.md` y `scripts/README.md`: la adquisición, la
retención 1, el catálogo completo (sin conteos fijos), el scheduler
`prefect-serve`, el `[5/5]` del asistente y la regla de rebuild. Se agregó la
tabla de las 12 variables `ACQUIRE_*`. Las notas de vigencia de
`docs/findings*.md` se actualizaron, pero sus mediciones fechadas se conservan
tal cual: son evidencia de su fecha.

**Guardas, una por cada clase de fallo que apareció:**

| Clase de fallo | Guarda |
|---|---|
| Faltaba una dep y se descubría a las 12 de la noche | `docker/Dockerfile.pipeline` importa `pyodbc`, `arrow_odbc`, `pyarrow`, `duckdb`, `prefect`, `httpx`, `fastapi` al final del build: si falta una, **rompe el build** |
| Recrear el contenedor sin rebuild (código viejo corriendo) | `scripts/verify_stack.sh` compara el `sha256` de `flow.py`, `serve.py`, `pipeline.py` y `retention.py` contra el contenedor, y si difieren imprime el comando de arreglo |
| Config de adquisición incompleta y silenciosa | `verify_stack.sh` valida deps, estado legible y exactamente un artefacto fechado; `configure.sh --check` reporta la adquisición |
| Variable que el código lee y nadie documenta | `tests/test_docs_drift.py`: toda `ACQUIRE_*` leída con `os.getenv` tiene que aparecer en `docs/MANUAL.md` |
| Conteos fijos en los docs | `tests/test_docs_drift.py`: `README.md`, `MANUAL.md` y `API.md` no pueden citar `485 557`, `7 tablas` ni `siete tablas` |

**Defecto nuevo que encontró la propia guarda.** `verify_stack.sh` mostró
`aplicado: ninguno` con el lago ya cargado. Causa: la corrida exitosa ingirió
**por el fallback de disco**, y `should_mark_applied` sólo miraba el outcome de
`acquire`, así que `applied` quedaba en `None` y la guarda de antigüedad se
quedaba sin referencia: cualquier artefacto, incluso uno viejo, habría sido
aceptado.

Corrección: `bak_extract` devuelve `{status, source, detail}` y el flow marca
como aplicado **la ruta que realmente se ingirió** (`applied_id_for`), sólo si
la extracción ocurrió. Marcar el candidato cuando la extracción se saltó
registraría como aplicado algo que no entró al lago.

Queda documentado un límite conocido: la guarda sólo tiene referencia después
de la primera extracción real. Si el estado se pierde, hay que reseedarlo; el
script de siembra es tres líneas sobre `mark_applied`.

### L15 — El MCP estaba caído y mentía en las instrucciones

**Origen.** Pregunta del usuario: "¿está corriendo el MCP?". No: el servicio
`lake-api` (REST + MCP en `/mcp/sse`) nunca se levantó después del rename del
proyecto. Quedaba un `dulse-lake-api` apagado hacía 4 días, con el nombre viejo.

**Defecto 1 — instrucciones falsas al agente.** `mcp/server.py` entrega un
`instructions` que el agente lee al conectarse; decía "esquema bronze:
`mssql_bs_*` y `mssql_d_mart_*`… las 7 tablas reales más los metadatos de dlt".
Levantarlo así habría arrancado a cualquier agente con una premisa falsa sobre
el contenido del lago. Es la misma deriva de L14, pero en código: el test de
L14 sólo miraba `README/MANUAL/API.md`. Ahora cubre también las fuentes que lee
un modelo (`mcp/server.py`, `duckdb_engine.py`).

**Defecto 2 — la frescura no existía.** `sqlserver_tables()` documentaba
`last_load_id` para saber si una tabla estaba fresca, y lo leía de
`_dlt_load_id`. Verificado contra el catálogo: **0 de 788** tablas `mssql_*`
tienen esa columna, porque el loader actual escribe Parquet con DuckDB y dlt se
retiró. Devolvía `None` en todas, sin error.

Peor: `tests/test_api_mcp.py` **afirmaba que `None` era lo esperado**. El test
cuidaba el defecto. Eso apareció al correr el suite completo, no por leer el
código.

Corrección: la frescura sale de los snapshots de DuckLake, que es el registro
real de escrituras. Como la ingesta reemplaza la tabla entera (drop + create),
el último snapshot que la creó es su última escritura. `last_load_id` pasa a
llevar el snapshot y se agrega `last_load_at`. Sin cambio de formato de datos.

Cubierto por `tests/test_frescura_inventario.py`, contra un catálogo DuckLake
temporal de verdad: tabla escrita una vez, tabla recreada (gana el snapshot más
reciente), varias tablas, y lago sin escrituras.

**Defecto 3 — `.env` no era "sourceable".** `configure_env.py` escribía
`clave=valor` sin comillas. `ACQUIRE_SUBJECT_PATTERN=Backup de Base de Datos
Disponible` hacía que `set -a; source .env` dejara la variable en `Backup` y
tratara `de`, `Base`… como comandos. Invisible porque el filtro IMAP matchea por
subcadena. Ahora se citan los valores que lo necesitan, con **comillas simples**:
son las únicas que bash y docker compose tratan igual (compose las quita sin
expandir, bash no expande nada adentro). Verificado con los dos lectores y con
un contenedor real.

**Guarda de servicio.** `scripts/verify_stack.sh` ahora cubre `lake-api`:
corriendo, `/health` 200, `/mcp/sse` 401 sin credencial (arriba y fail-closed,
sin manejar secretos en el script) y código del contenedor igual al repo. Nadie
avisaba cuando la API estaba caída; ahora el script falla.

**Verificación en vivo.** Handshake MCP real con `fastmcp`: 6 herramientas
(`sqlserver_tables`, `list_tables`, `describe_table`, `preview_rows`,
`run_query`, `list_snapshots`), `preview_rows(mssql_bs_moneda, 2)` y
`describe_table` devolviendo datos reales, e `instructions` nuevas servidas por
el contenedor.
