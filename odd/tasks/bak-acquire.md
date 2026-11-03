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
2. Debe existir una política de retención que impida el crecimiento sin techo:
   cada backup pesa ~7,5 GB y llegan 1–2 por semana (~60 GB/mes).

### S9 — Fallas visibles

1. Un fallo de adquisición no debe ser silencioso: la corrida debe quedar en
   estado fallido en Prefect y dejar el motivo.

## Tasks

| ID | Specs | Qué | Ruta | Commit |
|---|---|---|---|---|
| T1 | S3 | Identidad del backup: parseo del nombre + guarda de monotonicidad | `orchestration/kavos_acquire/identity.py` | `ce4ef90` |
| T2 | S5 | Estado persistido: JSON atómico, Message-IDs, timestamps aplicados | `orchestration/kavos_acquire/state.py` | `4fbbc45` |
| T3 | S2,S4,S8 | Descarga reanudable, checksum, guarda de ventana y de disco, commit atómico | `orchestration/kavos_acquire/download.py` | `fa326ee` |
| T4 | S6 | Poller IMAP de Gmail: filtro por remitente/asunto, extracción de links, allowlist | `orchestration/kavos_acquire/mailbox.py` | `d53aed4` |
| T5 | — | Resolver pluggable por host (directo/S3, OneDrive, Drive) | `orchestration/kavos_acquire/resolvers.py` | `dc7a8ba` |
| T6 | S7 | Tarea `acquire` en el flow + forma de correrlo con schedule | `orchestration/flow.py`, `orchestration/serve.py` | `c9233a6`, `ea9c816` |
| T7 | S8 | Poda de backups reteniendo N | `scripts/prune_backups.py` | `87a27cd` |
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
