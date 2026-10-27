# Panel de acceso: SSR + HTMX, reseteo de passwords y activación por link

Rebanada sobre la feature de credenciales (PR #2). Convierte el panel de una SPA mínima
en HTML server-rendered con Jinja2 + HTMX, muda la ruta a `/auth/panel` y agrega el ciclo
completo de gestión de passwords.

## Specs

S1. "No se debe llamar solo /ui sino algo más descriptivo."
    → Ruta del panel: `/auth/panel`. `/ui` queda como redirección 308 permanente.

S2. "Como Admin quiero poder resetear contraseñas de usuario."
    → superAdmin resetea la password de cualquier usuario. Decisión del usuario:
    el reset **corta sólo las sesiones del panel** de ese usuario; las API keys y los
    JWT de datos siguen vivos.

S3. "Cuando se crea un usuario o se resetea la contraseña quiero que cuando se inicia
    sesion obligatoriamente el usuario debe cargar una nueva."
    → Toda credencial emitida por el admin es **de un solo uso**. Dos caminos:
    link de activación (el usuario define su password) o password temporal aleatoria
    (se muestra una vez y `must_change_password` obliga a cambiarla al entrar).

S4. "Cuando se genera el usuario la posibilidad de generar una password aleatoria."
    → Al crear o resetear: link de activación **o** password aleatoria generada con
    `secrets`, mostrada una sola vez con botón de copiar.

S5. "Quiero que se mejore la ux de todas las vistas para ser mejor."
    → Vistas con jerarquía clara, estados vacíos explícitos, errores junto al campo,
    confirmación en acciones destructivas y un mensaje de éxito por acción.

S6. "Implementar htmx para darle dinamismo a la ui."
    → HTMX 2.0.4 vendorizado en `api/static/htmx.min.js` (sin CDN: el stack corre
    offline). Tablas de keys y usuarios se refrescan por fragmentos, sin recargar.

S7. "Pasar a un server siderender."
    → Jinja2 en FastAPI: los templates arman el HTML con los datos ya adentro. El
    navegador no pide JSON ni construye el DOM.

S8. "Verificar las rutas para que no se pise el front de la API/MCP"
    → Test que verifica: `/auth/panel` no captura `POST /auth/token`, `/api/v1/*`,
    `/mcp`, `/docs`; no hay path+method duplicados; `/ui` redirige.

## Tasks

| ID | Specs | Ruta | Commit |
| --- | --- | --- | --- |
| T1 | — | Documento de feature + tablero | `docs(ui): ...` |
| T2 | S7, S6 | Jinja2 + HTMX vendorizado + layout base | `feat(ui): ...` |
| T3 | S2, S3, S4 | `auth_db`: flag de cambio, reset, activación, revocar sesiones, password aleatoria | `feat(auth): ...` |
| T4 | S3, S7 | SSR de entrar / salir / primera configuración / cambio obligatorio | `feat(ui): ...` |
| T5 | S2, S3 | Link de activación (un solo uso, 24 h) | `feat(ui): ...` |
| T6 | S5, S6, S7 | Panel de keys con HTMX | `feat(ui): ...` |
| T7 | S2, S4, S5, S6 | Administración de usuarios con HTMX | `feat(ui): ...` |
| T8 | S1, S8 | Colisión de rutas + `/ui` → `/auth/panel` + borrar la SPA vieja | `refactor(ui): ...` |
| T9 | S1–S8 | Documentación y bitácora | `docs(ui): ...` |
| T10 | — | Verificación integral y review nativo | `chore(review): ...` |

## Log

L1. Pedido original, textual:

> Quiero que trabajemos mejoras en la ui.
> 1- No se debe llamar solo /ui sino algo más descriptivo.
> 2- Como Admin quiero poder resetear contraseñas de usuario.
> 3- Cuando se crea un usuario o se resetea la contraseña quiero que cuando se inicia
> sesion obligatoriamente el usuario debe cargar una nueva.
> 4- Cuando se genera el usuario la posibilidad de generar una password aleatoria.
> 5- Quiero que se mejore la ux de todas las vistas para ser mejor.
> 6- Implementar htmx para darle dinamismo a la ui.
> 7- Pasar a un server siderender.
> 8- Verificar las rutas para que no se pise el front de la API/MCP

L2. Decisiones del usuario (cuestionario de 3 preguntas):
    - Ruta: `/auth/panel` (conviviendo con `POST /auth/token`).
    - Reset de password: revoca **sólo** las sesiones del panel.
    - Credencial temporal: **link de activación** de un solo uso.
    Combinado con S4, el admin elige entre link y password aleatoria en cada alta y reset.

L3. Estado de partida: 69 tests verdes, candidato de la feature de credenciales sin
    aprobar (lineage `review-ea07e75ca91696d4` en `escalated`). Este trabajo arranca
    sobre ese árbol y se revisará como rebanada propia.
