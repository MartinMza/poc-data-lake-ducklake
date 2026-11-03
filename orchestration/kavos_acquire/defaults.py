"""Valores por defecto del proveedor del backup (Finnegans).

Van en código y no solo en `.env.example` por una razón concreta: los valores
que había para remitente y asunto no matcheaban el aviso real, así que el
poller **nunca** habría descargado nada y el fallo era silencioso (una corrida
exitosa sin backups nuevos). Poner el default en un solo lugar, verificado
contra el correo real por `tests/test_mailbox.py`, hace que el caso normal
funcione sin configuración y que el default no pueda divergir del proveedor sin
que un test lo note.

El entorno sigue mandando: `ACQUIRE_FROM` y `ACQUIRE_SUBJECT_PATTERN` pisan
estos valores cuando están definidos.

Hechos verificados contra el aviso real (SES en nombre de Finnegans):

- `From: Finnegans Go <info@finnegans.com.ar>` — el dominio es `.com.ar`.
- `Subject: Backup de Base de Datos Disponible`
- El cuerpo `text/plain` **no** trae el link; sólo el `text/html`, con el `href`
  entre comillas simples y la URL cortada por quoted-printable.
"""

from __future__ import annotations

# Dirección exacta del aviso. Se compara por igualdad, no por sufijo.
FINNEGANS_SENDER = "info@finnegans.com.ar"

# Asunto del aviso, evaluado como regex sobre el Subject del correo.
FINNEGANS_SUBJECT_PATTERN = "Backup de Base de Datos Disponible"
