"""T6 / Specs S1, S3 y S9: el orquestador que une buzón, guarda y descarga.

Estos tests son el contrato que consume `flow.py`. La distinción central que
fijan: qué fallo se reintenta y qué fallo avanza el estado.

- **Terminal** (link vencido, host no soportado, artefacto más viejo): el estado
  avanza para no quedar trabado en el mismo correo para siempre, y la corrida
  falla para que se vea.
- **Transitorio** (red, integridad): el estado **no** avanza, así el próximo
  intento reanuda el `.part` y vuelve a probar.
"""

from __future__ import annotations

import base64
import struct
import zlib
from datetime import datetime, timezone

import httpx
import pytest

from kavos_acquire.identity import parse_backup_id
from kavos_acquire.pipeline import (
    AcquisitionFailed,
    acquire_latest_bak,
    mark_applied,
)
from kavos_acquire.state import load_state

BUCKET_HOST = "finnegans-virginia-db-backup-diario.s3.us-east-1.amazonaws.com"
REMOTE = "backup@finnegans.com"
PERMITIDOS = [BUCKET_HOST]

NUEVO = "FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz"
VIEJO = "FAF12_Kavos_2026_10_07_233301_411720.bak"
PAYLOAD = b"BAK-NUEVO" * 500

# La firma sintética se emite el 2026-11-02T09:00:05Z con ventana de 12 h.
# `AHORA` cae una hora después, bien adentro de la ventana: los tests no deben
# depender del reloj de la máquina que los corre.
AHORA = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


def _s3_url(nombre: str = NUEVO, *, host: str = BUCKET_HOST, fecha: str = "20261102T090005Z") -> str:
    return (
        f"https://{host}/2026-11-01/{nombre}"
        f"?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Date={fecha}"
        f"&X-Amz-Expires=43200&X-Amz-Signature=abc&X-Amz-Security-Token=t"
    )


def _crc(payload: bytes) -> str:
    return base64.b64encode(struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)).decode()


def _handler(payload: bytes = PAYLOAD, *, estado: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if estado != 200:
            return httpx.Response(estado)
        cabeceras = {"x-amz-checksum-crc32": _crc(payload)}
        rango = request.headers.get("range")
        if rango:
            inicio = int(rango.removeprefix("bytes=").split("-")[0])
            if inicio >= len(payload):
                return httpx.Response(416, headers=cabeceras)
            cuerpo = payload[inicio:]
            cabeceras["content-range"] = f"bytes {inicio}-{len(payload) - 1}/{len(payload)}"
            return httpx.Response(206, content=cuerpo, headers=cabeceras)
        return httpx.Response(200, content=payload, headers=cabeceras)

    return handler


def _mail(url: str, *, uid: int = 4, asunto: str = "Backup diario FAF12_Kavos") -> bytes:
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["From"] = f'"Finnegans Backup" <{REMOTE}>'
    msg["Subject"] = asunto
    msg["Message-ID"] = f"<CAB-{uid}@finnegans.com>"
    msg.set_content(f"Bajalo de acá: {url}")
    return msg.as_bytes()


class FakeImap:
    def __init__(self, mensajes: dict[int, bytes]):
        self.mensajes = mensajes
        self.seleccion: tuple[str, bool] | None = None
        self.cerrado = False

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        self.seleccion = (mailbox, readonly)
        return ("OK", [b"1"])

    def uid(self, comando: str, *args):
        if comando == "SEARCH":
            return ("OK", [" ".join(str(u) for u in sorted(self.mensajes)).encode()])
        if comando == "FETCH":
            crudo = self.mensajes[int(args[0])]
            return ("OK", [("1 (RFC822 {0}".format(len(crudo)).encode(), crudo), b")"])
        raise AssertionError(comando)

    def logout(self):
        self.cerrado = True


CONFIG = dict(
    imap_user="bot@kavos.com",
    imap_password="app-password-de-prueba",
    sender=REMOTE,
    subject_pattern=r"Backup diario",
    link_hosts=PERMITIDOS,
)


def _acquire(tmp_path, imap, handler, **extra):
    extra.setdefault("now", AHORA)
    return acquire_latest_bak(
        state_path=tmp_path / "state.json",
        incoming_dir=tmp_path / "incoming",
        final_dir=tmp_path / "mssql-backup",
        imap_client=imap,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        **CONFIG,
        **extra,
    )


# --- camino feliz ------------------------------------------------------------


def test_descarga_el_backup_y_avanza_el_estado(tmp_path):
    imap = FakeImap({4: _mail(_s3_url())})

    resultado = _acquire(tmp_path, imap, _handler())

    assert resultado.status == "acquired"
    assert resultado.path == tmp_path / "mssql-backup" / NUEVO
    assert resultado.path.read_bytes() == PAYLOAD
    assert resultado.backup_id.filename == NUEVO

    estado = load_state(tmp_path / "state.json")
    assert estado.last_uid == 4
    assert estado.processed_message_ids == ["<CAB-4@finnegans.com>"]
    # El backup descargado NO cuenta como aplicado: eso lo marca la ingesta.
    assert estado.applied is None


def test_el_buzon_se_lee_en_readonly(tmp_path):
    imap = FakeImap({4: _mail(_s3_url())})

    _acquire(tmp_path, imap, _handler())

    assert imap.seleccion == ("INBOX", True)


# --- nada que hacer ---------------------------------------------------------


def test_sin_correo_nuevo_no_hace_nada(tmp_path):
    resultado = _acquire(tmp_path, FakeImap({}), _handler())

    assert resultado.status == "no_new_backup"
    assert resultado.path is None


def test_un_link_fuera_de_la_allowlist_no_se_descarga(tmp_path):
    ajeno = _s3_url(host="evil.example.net")

    with pytest.raises(AcquisitionFailed) as exc:
        _acquire(tmp_path, FakeImap({4: _mail(ajeno)}), _handler())

    assert "allowlist" in str(exc.value)
    assert not (tmp_path / "mssql-backup" / NUEVO).exists()
    # Terminal: el estado avanza para no quedar trabado en ese correo.
    assert load_state(tmp_path / "state.json").last_uid == 4


# --- S3: no retroceder -------------------------------------------------------


def test_un_backup_mas_viejo_que_el_aplicado_no_pisa_el_lago(tmp_path):
    mark_applied(tmp_path / "state.json", parse_backup_id(NUEVO))

    with pytest.raises(AcquisitionFailed) as exc:
        _acquire(tmp_path, FakeImap({5: _mail(_s3_url(VIEJO), uid=5)}), _handler())

    assert "antigüedad" in str(exc.value)
    assert not (tmp_path / "mssql-backup" / VIEJO).exists()
    assert load_state(tmp_path / "state.json").last_uid == 5


def test_mark_applied_persiste_y_el_mismo_artefacto_queda_rechazado(tmp_path):
    mark_applied(tmp_path / "state.json", parse_backup_id(NUEVO))

    assert load_state(tmp_path / "state.json").applied == parse_backup_id(NUEVO)
    with pytest.raises(AcquisitionFailed):
        _acquire(tmp_path, FakeImap({6: _mail(_s3_url(NUEVO), uid=6)}), _handler())


# --- qué se reintenta y qué no ----------------------------------------------


def test_un_link_vencido_registra_el_avance_y_falla(tmp_path):
    imap = FakeImap({4: _mail(_s3_url())})

    with pytest.raises(AcquisitionFailed) as exc:
        _acquire(
            tmp_path,
            imap,
            _handler(),
            now=datetime(2026, 11, 3, 4, 18, tzinfo=timezone.utc),
        )

    assert "link" in str(exc.value).lower()
    # Terminal: si no avanzara, cada corrida quedaría trabada en el mismo correo.
    assert load_state(tmp_path / "state.json").last_uid == 4


def test_un_error_de_red_no_avanza_el_estado(tmp_path):
    imap = FakeImap({4: _mail(_s3_url())})

    with pytest.raises(AcquisitionFailed):
        _acquire(tmp_path, imap, _handler(estado=500))

    estado = load_state(tmp_path / "state.json")
    assert estado.last_uid == 0
    assert estado.processed_message_ids == []


def test_un_checksum_roto_no_avanza_el_estado(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        cabeceras = {"x-amz-checksum-crc32": "AAAAAAAAAAAAAAAAAAAAAA=="}
        rango = request.headers.get("range")
        if rango:
            cabeceras["content-range"] = f"bytes 0-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
            return httpx.Response(206, content=PAYLOAD, headers=cabeceras)
        return httpx.Response(200, content=PAYLOAD, headers=cabeceras)

    with pytest.raises(AcquisitionFailed):
        _acquire(tmp_path, FakeImap({4: _mail(_s3_url())}), handler)

    assert load_state(tmp_path / "state.json").last_uid == 0
    assert not (tmp_path / "mssql-backup" / NUEVO).exists()


def test_si_falla_el_ultimo_correo_pero_hay_uno_bueno_antes_se_usa_el_bueno(tmp_path):
    # El correo 6 trae un link muerto, el 4 trae uno válido: se baja el 4.
    imap = FakeImap(
        {
            4: _mail(_s3_url(NUEVO), uid=4),
            6: _mail(_s3_url(host="evil.example.net"), uid=6),
        }
    )

    resultado = _acquire(tmp_path, imap, _handler())

    assert resultado.status == "acquired"
    assert resultado.backup_id.filename == NUEVO
