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
| T11 | S5 | No poder auto-deshabilitarse (feedback de diseño) | `fix(ui): ...` |

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

L4. Implementado en dos rebanadas, cada una con su verificación:

    - `7e83981` capa de datos (10 tests nuevos).
    - `3cd57af` panel SSR completo: templates, rutas, borrado de la SPA vieja,
      deps (jinja2, python-multipart) y 24 tests nuevos. Frontera de rebanada:
      todo lo que va de `7e83981` a `3cd57af` es una sola unidad de UI; no se
      parte más chico porque los templates y sus rutas comparten el mismo
      contrato de contexto.

L5. Estado por spec al cerrar la implementación:

| Spec | Estado | Evidencia |
| --- | --- | --- |
| S1 | hecho | `/auth/panel`; `/ui` → 308 |
| S2 | hecho | reset con link o temporal; corta sólo sesiones del panel |
| S3 | hecho | temporal fuerza cambio; el link define la contraseña |
| S4 | hecho | `generate_random_password` + opción en el alta y el reset |
| S5 | hecho | vistas con jerarquía, estados vacíos, confirmaciones, errores inline |
| S6 | hecho | HTMX 2.0.4 vendorizado; tablas por fragmentos |
| S7 | hecho | Jinja2 server-side, sin JSON intermedio ni DOM a mano |
| S8 | hecho | test de convivencia de rutas + no duplicados |

L6. Verificación: 87 tests verdes y 26 comprobaciones en vivo contra el
    contenedor `lake-api` (login, link, activación, temporal, cambio forzado,
    reset, keys, rotación, revocación, deshabilitado, docs y API intactas).

L7. Feedback de diseño sobre `/auth/panel/usuarios`, textual:

> Como admin no quiero poder auto deshabilitarme. porque sino nunca puedo entrar de nuevo.

Se abre T11: bloquear la auto-deshabilitación en el servidor y dejar de ofrecer
el botón en la fila propia (y en cualquier superAdmin, que la base ya rechaza),
para que la UI no prometa lo que el servidor no permite.

L8. T11 cerrado. Dos capas, porque la UI sola no alcanza:

    | Capa | Antes | Ahora |
    | --- | --- | --- |
    | servidor | dejaba pasar y la base fallaba con "No existe el usuario, o es el superAdmin" | 400 "No podés deshabilitar tu propia cuenta." |
    | base | mismo mensaje confuso para cualquier caso | "No existe el usuario." / "Un superAdmin no se puede deshabilitar desde el panel." |
    | UI | botón presente en la fila propia y en superAdmins | fila propia → "tu cuenta" + "Mi contraseña"; superAdmin → sin botón |

    Verificado en vivo: auto-deshabilitado 400 y la cuenta sigue activa con la
    sesión intacta; otro superAdmin 400; un `user` común 200. 89 tests verdes.

L9. Pedido nuevo, textual:

> 1- Quiero que en los formularios donde este la contraseña, para crear y poner la
> contraseña, como usuario quiero poder ver la contraseña con un boton de ver contraseña.
> 2- El swagger tiene que estar oculto y solo poder verse si esta loggeado

T12 (S10): botón **Ver/Ocultar** en todos los campos de contraseña (entrar,
primera configuración, activación y cambio de contraseña). Un solo handler por
delegación en `base.html`, sin dependencias: alterna `type` y `aria-pressed`.

T13 (S9): la documentación deja de ser pública. `docs_url`, `redoc_url` y
`openapi_url` se apagan en FastAPI y se sirven con guarda propia: sin sesión del
panel, 303 a `/auth/panel/entrar?next=<ruta>`. El `next` se valida (sólo rutas
locales, sin `//` ni `://`) y una contraseña temporal pendiente manda sobre él.

L10. Pedido, textual: "agregar la validacion de los nombres".

T14 (S11): `validate_username` en `auth_db` — 3 a 64 caracteres, sólo letras,
números, punto, guion y guion bajo — aplicada en `create_user`, que es el único
punto de alta (panel, primera configuración y migración del `.env`).

- los nombres inválidos que ya existen **no se renombran solos**: el arranque los
  lista con el `UPDATE` sugerido (`invalid_usernames`), porque cambiar una
  identidad es decisión del humano;
- la migración del entorno omite un nombre inválido y lo anota en vez de romper
  el arranque;
- los formularios llevan `pattern` + `title` para avisar antes de enviar.

4 tests nuevos (100 en total).
