"""T5: resolución del link por host.

El caso real es S3 presignado: no hay nada que resolver, la URL se descarga
tal cual. Los otros resolvers existen para que el día que Finnegans cambie de
proveedor el pipeline no esté atado a un host.

WeTransfer **no** se implementa como scraper. Su descarga depende de un
endpoint no documentado que ya cambió varias veces; shipear eso sin poder
probarlo contra el servicio real sería peor que fallar cerrado con un mensaje
que pida S3 o SharePoint. `classify` igual lo reconoce, para que el operador
reciba la razón exacta y no un "host desconocido".
"""

from __future__ import annotations

import httpx
import pytest

from kavos_acquire.resolvers import (
    ResolverKind,
    UnsupportedLinkHost,
    classify,
    resolve,
)

S3_URL = (
    "https://finnegans-virginia-db-backup-diario.s3.us-east-1.amazonaws.com/"
    "2026-11-01/FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz"
    "?X-Amz-Date=20261102T090005Z&X-Amz-Expires=43200&X-Amz-Signature=abc"
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _sin_red(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"no se esperaba request a {request.url.path}")


# --- clasificación -----------------------------------------------------------


def test_s3_presignado_es_descarga_directa():
    assert classify(S3_URL) is ResolverKind.DIRECT


def test_drive_se_reconoce():
    assert classify("https://drive.google.com/file/d/ABC123/view?usp=sharing") is ResolverKind.GDRIVE


def test_sharepoint_se_reconoce():
    assert (
        classify("https://kavos-my.sharepoint.com/:u:/g/personal/x/EY123?e=abcd")
        is ResolverKind.ONEDRIVE
    )


def test_wetransfer_se_reconoce_para_fallar_con_motivo():
    assert classify("https://we.tl/t-abcdef") is ResolverKind.WETRANSFER


def test_un_host_https_desconocido_es_descarga_directa():
    # La confianza la decide la allowlist de `mailbox`, no el resolver. Una sola
    # lista de hosts: con dos, tarde o temprano se desincronizan.
    assert classify("https://backup.mi-empresa.com/x.bak") is ResolverKind.DIRECT


def test_http_no_se_acepta():
    with pytest.raises(UnsupportedLinkHost):
        classify("http://finnegans-virginia-db-backup-diario.s3.amazonaws.com/x.bak")


# --- resolución --------------------------------------------------------------


def test_directo_no_toca_la_red_y_devuelve_la_misma_url():
    llamadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas.append(request)
        return httpx.Response(200, content=b"")

    assert resolve(S3_URL, client=_client(handler)) == S3_URL
    assert llamadas == []


def test_onedrive_agrega_download_uno_sin_pisar_la_query():
    url = "https://kavos-my.sharepoint.com/:u:/g/personal/x/EY123?e=abcd"

    resuelta = resolve(url, client=_client(_sin_red))

    assert "download=1" in resuelta
    assert "e=abcd" in resuelta


def test_onedrive_no_duplica_download_si_ya_estaba():
    url = "https://kavos-my.sharepoint.com/:u:/g/personal/x/EY123?e=abcd&download=1"

    assert resolve(url, client=_client(_sin_red)) == url


def test_drive_convierte_el_link_de_vista_en_descarga():
    def handler(request: httpx.Request) -> httpx.Response:
        # Sin interstitial: es un archivo chico y responde el contenido.
        return httpx.Response(200, content=b"x", headers={"content-type": "application/octet-stream"})

    resuelta = resolve(
        "https://drive.google.com/file/d/ABC123/view?usp=sharing",
        client=_client(handler),
    )

    assert "id=ABC123" in resuelta
    assert "export=download" in resuelta
    assert "/file/d/" not in resuelta


def test_drive_grande_resuelve_el_interstitial_de_confirmacion():
    formulario = (
        "<html><body><p>Google Drive can't scan this file for viruses.</p>"
        '<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">'
        '<input type="hidden" name="id" value="ABC123">'
        '<input type="hidden" name="export" value="download">'
        '<input type="hidden" name="confirm" value="t">'
        '<input type="hidden" name="uuid" value="9f8e-1234">'
        "</form></body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "confirm=" in str(request.url):
            return httpx.Response(200, content=b"y", headers={"content-type": "application/octet-stream"})
        return httpx.Response(
            200, content=formulario.encode(), headers={"content-type": "text/html"}
        )

    resuelta = resolve(
        "https://drive.google.com/file/d/ABC123/view?usp=sharing",
        client=_client(handler),
    )

    assert resuelta.startswith("https://drive.usercontent.google.com/download?")
    assert "confirm=t" in resuelta
    assert "uuid=9f8e-1234" in resuelta
    assert "id=ABC123" in resuelta


def test_wetransfer_falla_con_un_mensaje_accionable():
    with pytest.raises(UnsupportedLinkHost) as exc:
        resolve("https://we.tl/t-abcdef", client=_client(_sin_red))

    mensaje = str(exc.value)
    assert "WeTransfer" in mensaje
    assert "S3" in mensaje or "SharePoint" in mensaje
