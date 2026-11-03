"""T4 / Specs S1 y S6: poller de Gmail y allowlist de destinos.

Sin red: `imaplib` se reemplaza por un doble que replica su forma real
(`uid("SEARCH", …)` → `(status, [b"1 2 3"])`, `uid("FETCH", …)` → tuplas con
`(RFC822)`), así lo que se prueba es el mismo código que corre contra Gmail.

Trampas reales que estos tests fijan, y que un scraper ingenuo se come:

- En HTML los `&` llegan escapados como `&amp;`, y la URL presignada está llena
  de `&`. Sin desescapar, la firma queda rota.
- Quoted-printable mete cortes de línea con `=` dentro de la URL.
- `backup@finnegans.com` no debe matchear `backup@finnegans.com.atacante.net`.
"""

from __future__ import annotations

import httpx
import pytest

from kavos_acquire.mailbox import (
    LinkCandidate,
    host_allowed,
    parse_message,
    safe_https_url,
    scan_mailbox,
    sender_matches,
    subject_matches,
)

REMOTE = "backup@finnegans.com"
BUCKET_HOST = "finnegans-virginia-db-backup-diario.s3.us-east-1.amazonaws.com"
URL = (
    f"https://{BUCKET_HOST}/2026-11-01/"
    "FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Date=20261102T090005Z"
    "&X-Amz-Expires=43200"
    "&X-Amz-Signature=0123456789abcdef"
)


# --- remitente y asunto ------------------------------------------------------


def test_el_remitente_se_compara_por_direccion_exacta():
    assert sender_matches('"Finnegans Backup" <backup@finnegans.com>', REMOTE)
    assert sender_matches("Backup@Finnegans.com", REMOTE)


def test_un_dominio_que_solo_empieza_igual_no_matchea():
    # Sufijo no es igualdad: este es el spoof clásico.
    assert not sender_matches("backup@finnegans.com.atacante.net", REMOTE)
    assert not sender_matches("backup@notfinnegans.com", REMOTE)


def test_el_asunto_se_evalua_como_regex():
    assert subject_matches("Backup diario FAF12_Kavos 2026-11-01", r"Backup diario")
    assert not subject_matches("Novedades de producto", r"Backup diario")


# --- allowlist de hosts (S6) -------------------------------------------------


def test_el_host_exacto_esta_permitido():
    assert host_allowed(BUCKET_HOST, [BUCKET_HOST])


def test_otro_bucket_de_amazonaws_queda_afuera():
    # Sonar `*.amazonaws.com` como comodín libre permitiría cualquier bucket.
    assert not host_allowed("bucket-de-otro.s3.us-east-1.amazonaws.com", [BUCKET_HOST])


def test_el_comodin_solo_cubre_el_sufijo_declarado():
    assert host_allowed("x.mi-dominio.com", ["*.mi-dominio.com"])
    assert not host_allowed("mi-dominio.com.atacante.net", ["*.mi-dominio.com"])


def test_solo_https_sin_credenciales_embebidas():
    assert safe_https_url(URL) is True
    assert safe_https_url(URL.replace("https://", "http://")) is False
    assert safe_https_url(f"https://user:pass@{BUCKET_HOST}/x.bak") is False


# --- parseo del mensaje ------------------------------------------------------


def _raw_message(body_plain: str, body_html: str | None = None) -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = f'"Finnegans Backup" <{REMOTE}>'
    msg["Subject"] = "Backup diario FAF12_Kavos 2026-11-01"
    msg["Message-ID"] = "<CAB-1@finnegans.com>"
    msg.set_content(body_plain)
    if body_html is not None:
        msg.add_alternative(body_html, subtype="html")
    return msg.as_bytes()


def test_parsea_metadatos_y_cuerpo_texto():
    parsed = parse_message(_raw_message(f"Bajalo de acá: {URL}"))

    assert parsed.message_id == "<CAB-1@finnegans.com>"
    assert parsed.sender == REMOTE
    assert parsed.subject.startswith("Backup diario")
    assert URL in parsed.text


def test_desescapa_el_ampersand_del_html():
    html = f'<p><a href="{URL.replace("&", "&amp;")}">descargar</a></p>'

    parsed = parse_message(_raw_message("mirá el html", html))

    assert URL in parsed.links
    assert "&amp;" not in parsed.links[0]


def test_reconstruye_una_url_cortada_por_quoted_printable():
    # Cuerpo codificado de verdad con quopri: los `=` de la URL pasan a `=3D` y
    # el encoder mete cortes suaves (`=\n`) porque la URL supera los 76 chars.
    import quopri

    cuerpo = f"Link: {URL}".encode("utf-8")
    codificado = quopri.encodestring(cuerpo)
    assert b"=\n" in codificado, "el fixture debe tener un corte suave"

    crudo = (
        f"From: Finnegans <{REMOTE}>\r\n"
        f"Subject: Backup diario\r\n"
        f"Message-ID: <CAB-2@finnegans.com>\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: quoted-printable\r\n"
        f"\r\n"
    ).encode("utf-8") + codificado

    parsed = parse_message(crudo)

    assert URL in parsed.links


def test_sin_message_id_se_genera_uno_estable():
    crudo = (
        f"From: Finnegans <{REMOTE}>\r\n"
        f"Subject: Backup diario\r\n"
        f"Content-Type: text/plain\r\n"
        f"\r\n"
        f"{URL}\r\n"
    ).encode("utf-8")

    parsed = parse_message(crudo, fallback_id="uid:7")

    assert parsed.message_id == "uid:7"


# --- scan contra un IMAP falso ----------------------------------------------


class FakeImap:
    """Doble de `imaplib.IMAP4_SSL` con la forma de retorno real."""

    def __init__(self, mensajes: dict[int, bytes]):
        self.mensajes = mensajes
        self.criterios: list[str] = []

    def uid(self, comando: str, *args):
        if comando == "SEARCH":
            self.criterios.append(str(args[1]))
            encontrados = " ".join(str(uid) for uid in sorted(self.mensajes))
            return ("OK", [encontrados.encode()])
        if comando == "FETCH":
            uid = int(args[0])
            crudo = self.mensajes[uid]
            return ("OK", [("1 (RFC822 {0}".format(len(crudo)).encode(), crudo), b")"])
        raise AssertionError(f"comando inesperado: {comando} {args}")


CONFIG = dict(
    sender=REMOTE,
    subject_pattern=r"Backup diario",
    link_hosts=[BUCKET_HOST],
)


def test_scan_ignora_los_uid_ya_vistos():
    imap = FakeImap({5: _raw_message(f"viejo {URL}"), 9: _raw_message(f"nuevo {URL}")})

    candidatos = scan_mailbox(imap, since_uid=5, **CONFIG)

    assert [c.uid for c in candidatos] == [9]
    assert imap.criterios == ["UID 6:*"]


def test_scan_filtra_por_remitente_y_asunto():
    otro_remitente = (
        f"From: Otro <otro@example.com>\r\nSubject: Backup diario\r\n"
        f"Message-ID: <X@example.com>\r\n\r\n{URL}\r\n"
    ).encode()
    otro_asunto = (
        f"From: Finnegans <{REMOTE}>\r\nSubject: Newsletter\r\n"
        f"Message-ID: <Y@finnegans.com>\r\n\r\n{URL}\r\n"
    ).encode()

    imap = FakeImap({1: otro_remitente, 2: otro_asunto})

    assert scan_mailbox(imap, since_uid=0, **CONFIG) == []


def test_scan_marca_los_links_fuera_de_la_allowlist():
    ajeno = URL.replace(BUCKET_HOST, "evil.example.net")

    imap = FakeImap({3: _raw_message(f"mirá {ajeno}")})

    candidatos = scan_mailbox(imap, since_uid=0, **CONFIG)

    assert len(candidatos) == 1
    assert candidatos[0].allowed is False
    assert "evil.example.net" in candidatos[0].reason


def test_scan_devuelve_el_link_permitido():
    imap = FakeImap({4: _raw_message(f"descargá {URL}")})

    candidatos = scan_mailbox(imap, since_uid=0, **CONFIG)

    assert len(candidatos) == 1
    candidato = candidatos[0]
    assert isinstance(candidato, LinkCandidate)
    assert candidato.allowed is True
    assert candidato.url == URL
    assert candidato.message_id == "<CAB-1@finnegans.com>"
