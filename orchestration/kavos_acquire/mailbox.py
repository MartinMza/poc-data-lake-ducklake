"""Specs S1 y S6: poller del buzón y allowlist de destinos.

El buzón es Gmail por IMAP con app password. El poller **no** muta el buzón
(`SELECT` en modo readonly): la deduplicación la lleva el estado persistido por
`Message-ID`, no la bandera "leído". Marcar correos como leídos desde un
proceso automático es una forma silenciosa de romperle el buzón a una persona.

Dos capas de filtro, ambas obligatorias:

1. **Quién manda**: la dirección del remitente se compara por igualdad exacta.
   `backup@finnegans.com` y `backup@finnegans.com.atacante.net` son distintos.
2. **De dónde se baja**: solo hosts de la allowlist y solo `https` sin
   credenciales embebidas. Un poller que baja cualquier URL que aparezca en un
   correo es una descarga arbitraria con permisos de red.
"""

from __future__ import annotations

import email
import html as html_module
import imaplib
import logging
import re
from dataclasses import dataclass, field
from email.message import Message
from email.utils import parseaddr
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING = ".,;:)]}>\"'"


class MailboxError(RuntimeError):
    """Falló el acceso al buzón o su contenido no se pudo interpretar."""


@dataclass(frozen=True)
class LinkCandidate:
    """Un link encontrado en un correo, ya clasificado contra la allowlist."""

    uid: int
    message_id: str
    subject: str
    url: str
    allowed: bool
    reason: str = ""


@dataclass(frozen=True)
class ParsedMessage:
    """Lo único que el poller necesita de un correo."""

    message_id: str
    sender: str
    subject: str
    text: str = ""
    html: str = ""
    links: list[str] = field(default_factory=list)


def sender_matches(header_value: str, expected: str) -> bool:
    """Igualdad exacta sobre la dirección, ignorando el nombre visible."""
    _, direccion = parseaddr(header_value or "")
    return direccion.strip().lower() == expected.strip().lower()


def subject_matches(subject: str, pattern: str) -> bool:
    """El asunto se evalúa como regex, sin distinguir mayúsculas."""
    try:
        return re.search(pattern, subject or "", re.IGNORECASE) is not None
    except re.error as exc:
        raise MailboxError(f"patrón de asunto inválido {pattern!r}: {exc}") from exc


def host_allowed(host: str, allowlist: list[str]) -> bool:
    """Allowlist de hosts: entrada exacta, o `*.sufijo` con frontera de etiqueta.

    Deliberadamente **no** hay comodín libre: permitir `*.amazonaws.com` abriría
    cualquier bucket de cualquier cuenta.
    """
    actual = (host or "").strip().lower()
    if not actual:
        return False
    for entrada in allowlist:
        patron = entrada.strip().lower()
        if not patron:
            continue
        if patron.startswith("*."):
            sufijo = patron[1:]  # ".mi-dominio.com"
            if actual.endswith(sufijo) and len(actual) > len(sufijo):
                return True
        elif actual == patron:
            return True
    return False


def safe_https_url(url: str) -> bool:
    """Solo `https`, sin usuario/contraseña embebidos y con host."""
    partes = urlsplit(url)
    if partes.scheme.lower() != "https":
        return False
    if partes.username or partes.password:
        return False
    return bool(partes.hostname)


def extract_links(*cuerpos: str) -> list[str]:
    """Extrae URLs de los cuerpos, sin duplicar y preservando el orden.

    Desescapa entidades HTML antes de buscar: en el `href` los `&` vienen como
    `&amp;`, y una URL presignada sin los `&` reales tiene la firma rota.
    """
    vistos: set[str] = set()
    salida: list[str] = []
    for cuerpo in cuerpos:
        if not cuerpo:
            continue
        limpio = html_module.unescape(cuerpo)
        for crudo in _URL_RE.findall(limpio):
            url = crudo.rstrip(_TRAILING)
            if url and url not in vistos:
                vistos.add(url)
                salida.append(url)
    return salida


def _decode_part(part: Message) -> str:
    crudo = part.get_payload(decode=True)
    if crudo is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return crudo.decode(charset, errors="replace")
    except LookupError:
        return crudo.decode("utf-8", errors="replace")


def _bodies(msg: Message) -> tuple[str, str]:
    texto: list[str] = []
    html: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        subtipo = part.get_content_subtype()
        if part.get_content_type() == "text/plain":
            texto.append(_decode_part(part))
        elif part.get_content_type() == "text/html":
            html.append(_decode_part(part))
        elif subtipo and not subtipo.startswith("multipart"):
            continue
    return "\n".join(texto), "\n".join(html)


def parse_message(raw: bytes, *, fallback_id: str | None = None) -> ParsedMessage:
    """Parsea un correo crudo y devuelve solo lo que el poller usa."""
    msg = email.message_from_bytes(raw)
    message_id = (msg.get("Message-ID") or "").strip() or (fallback_id or "")
    texto, cuerpo_html = _bodies(msg)
    return ParsedMessage(
        message_id=message_id,
        sender=parseaddr(msg.get("From") or "")[1],
        subject=str(msg.get("Subject") or ""),
        text=texto,
        html=cuerpo_html,
        links=extract_links(texto, cuerpo_html),
    )


def _reason_for(url: str, link_hosts: list[str]) -> str:
    host = urlsplit(url).hostname or ""
    if not safe_https_url(url):
        return f"URL no segura (se exige https sin credenciales): {host or url}"
    if not host_allowed(host, link_hosts):
        return f"host fuera de la allowlist: {host}"
    return ""


def scan_mailbox(
    imap,
    *,
    since_uid: int,
    sender: str,
    subject_pattern: str,
    link_hosts: list[str],
) -> list[LinkCandidate]:
    """Busca correos con `uid > since_uid` y devuelve sus links clasificados.

    No descarga nada y no marca correos como leídos: solo lee y clasifica.
    """
    criterio = f"UID {since_uid + 1}:*"
    estado, datos = imap.uid("SEARCH", None, criterio)
    if estado != "OK":
        raise MailboxError(f"IMAP SEARCH falló ({estado}) con criterio {criterio!r}")

    crudos = b" ".join(datos or []).split()
    candidatos: list[LinkCandidate] = []
    for crudo in crudos:
        try:
            uid = int(crudo)
        except ValueError:
            continue
        if uid <= since_uid:
            continue
        estado, partes = imap.uid("FETCH", str(uid), "(RFC822)")
        if estado != "OK" or not partes or not isinstance(partes[0], tuple):
            raise MailboxError(f"IMAP FETCH falló para el uid {uid} ({estado})")
        mensaje = parse_message(partes[0][1], fallback_id=f"uid:{uid}")
        if not sender_matches(mensaje.sender, sender):
            logger.debug("uid %s: remitente %r no matchea", uid, mensaje.sender)
            continue
        if not subject_matches(mensaje.subject, subject_pattern):
            logger.debug("uid %s: asunto %r no matchea", uid, mensaje.subject)
            continue
        for url in mensaje.links:
            razon = _reason_for(url, link_hosts)
            candidatos.append(
                LinkCandidate(
                    uid=uid,
                    message_id=mensaje.message_id,
                    subject=mensaje.subject,
                    url=url,
                    allowed=not razon,
                    reason=razon,
                )
            )
    return candidatos


def connect_gmail(user: str, app_password: str, *, timeout: float = 60.0):
    """Abre y autentica una conexión IMAP contra Gmail.

    La app password nunca se loguea ni se incluye en una excepción.
    """
    if not user or not app_password:
        raise MailboxError(
            "faltan credenciales IMAP: definí ACQUIRE_IMAP_USER y "
            "ACQUIRE_IMAP_APP_PASSWORD en el entorno"
        )
    try:
        conexion = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT, timeout=timeout)
    except OSError as exc:
        raise MailboxError(f"no se pudo conectar a {GMAIL_IMAP_HOST}: {exc}") from exc
    try:
        conexion.login(user, app_password)
    except imaplib.IMAP4.error as exc:
        conexion.logout()
        raise MailboxError(
            f"IMAP rechazó las credenciales de {user}. En Gmail hace falta una "
            f"app password con verificación en dos pasos activa."
        ) from exc
    return conexion
