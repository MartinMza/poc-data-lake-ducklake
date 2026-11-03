"""T3 / Specs S2, S4, S5 y S8: descarga reanudable, integridad y commit atómico.

Nada de red real: `httpx.MockTransport` sirve de S3 y los tests controlan
`Range`, el checksum y los códigos de error.

Lo que estos tests fijan, en orden de gravedad:

- Una ventana de firma vencida **no** intenta descargar (S2): con 12 h de
  validez y 7,5 GB, reintentar un link muerto es tiempo perdido.
- Un checksum que no cierra **no** llega a `mssql-backup/` (S4): se queda como
  `.part` y se reporta.
- La URL nunca aparece en un mensaje de error ni en un log (S5).
- Sin espacio libre no se empieza (S8).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kavos_acquire.download import (
    DownloadError,
    DownloadIntegrityError,
    ExpiredLinkError,
    InsufficientSpaceError,
    crc32_as_s3,
    download,
    guard_window,
    parse_presign_window,
    sanitize_url,
)

PAYLOAD = b"BAK" * 1000
FILENAME = "FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz"

# URL sintética: misma *forma* que la real de Finnegans, con firma y token
# inventados. Nunca se usa la credencial verdadera en un test.
PRESIGNED = (
    "https://finnegans-virginia-db-backup-diario.s3.us-east-1.amazonaws.com/"
    f"2026-11-01/{FILENAME}"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=ASIAEXAMPLE%2F20261102%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20261102T090005Z"
    "&X-Amz-Expires=43200"
    "&X-Amz-Security-Token=TOKEN-INVENTADO"
    "&X-Amz-Signature=0123456789abcdef"
    "&x-amz-checksum-mode=ENABLED"
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _s3(payload: bytes = PAYLOAD, *, checksum: str | None = "auto", honor_range: bool = True):
    """Handler que se comporta como S3: 206 para Range, checksum en el header."""

    def handler(request: httpx.Request) -> httpx.Response:
        headers: dict[str, str] = {}
        if checksum == "auto":
            headers["x-amz-checksum-crc32"] = crc32_as_s3(payload)
        elif checksum is not None:
            headers["x-amz-checksum-crc32"] = checksum

        rango = request.headers.get("range")
        if rango and honor_range:
            inicio, _, fin = rango.removeprefix("bytes=").partition("-")
            start = int(inicio)
            if start >= len(payload):
                return httpx.Response(416, headers=headers)
            end = int(fin) if fin else len(payload) - 1
            cuerpo = payload[start : end + 1]
            headers["content-range"] = f"bytes {start}-{start + len(cuerpo) - 1}/{len(payload)}"
            return httpx.Response(206, content=cuerpo, headers=headers)

        return httpx.Response(200, content=payload, headers=headers)

    return handler


def _expired_now() -> datetime:
    # La firma tiene ventana de 12 h desde 2026-11-02T09:00:05Z.
    return datetime(2026, 11, 3, 4, 18, 16, tzinfo=timezone.utc)


# --- S5: la URL es un secreto ------------------------------------------------


def test_sanitize_url_descarta_toda_la_query():
    limpia = sanitize_url(PRESIGNED)

    assert limpia == (
        "https://finnegans-virginia-db-backup-diario.s3.us-east-1.amazonaws.com/"
        f"2026-11-01/{FILENAME}"
    )
    for prohibido in ("X-Amz", "Signature", "Security-Token", "?"):
        assert prohibido not in limpia


# --- S2: ventana de la firma -------------------------------------------------


def test_parsea_la_ventana_de_la_firma():
    ventana = parse_presign_window(PRESIGNED)

    assert ventana is not None
    assert ventana.signed_at == datetime(2026, 11, 2, 9, 0, 5, tzinfo=timezone.utc)
    assert ventana.expires_in == 43200


def test_un_link_sin_parametros_de_firma_no_tiene_ventana():
    assert parse_presign_window("https://example.com/x.bak") is None


def test_la_ventana_vencida_no_intenta_descargar(tmp_path):
    ventana = parse_presign_window(PRESIGNED)
    llamadas: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        llamadas.append(request)
        return httpx.Response(200, content=PAYLOAD)

    with pytest.raises(ExpiredLinkError) as exc:
        download(
            PRESIGNED,
            incoming_dir=tmp_path / "in",
            final_dir=tmp_path / "out",
            filename=FILENAME,
            client=_client(handler),
            now=_expired_now(),
        )

    assert llamadas == [], "no debe haber request con la firma vencida"
    assert not (tmp_path / "in").exists() or not list((tmp_path / "in").iterdir())
    assert "X-Amz" not in str(exc.value)


def test_la_ventana_corta_tampoco_alcanza():
    ventana = parse_presign_window(PRESIGNED)
    casi = ventana.signed_at + timedelta(seconds=ventana.expires_in - 60)

    with pytest.raises(ExpiredLinkError):
        guard_window(ventana, now=casi, min_remaining_seconds=1800)


def test_la_ventana_holgada_pasa():
    ventana = parse_presign_window(PRESIGNED)
    recien = ventana.signed_at + timedelta(minutes=5)

    assert guard_window(ventana, now=recien, min_remaining_seconds=1800) is None


# --- S4: integridad ----------------------------------------------------------


def test_crc32_como_lo_devuelve_s3():
    # Vectores independientes: S3 manda el CRC32 como base64 de 4 bytes BE.
    assert crc32_as_s3(b"") == "AAAAAA=="
    assert crc32_as_s3(b"hello") == "NhCmhg=="  # crc32 = 0x3610a686
    assert crc32_as_s3(PAYLOAD) == "owRX3A=="


def test_descarga_completa_en_una_pasada_commitea(tmp_path):
    destino = download(
        PRESIGNED,
        incoming_dir=tmp_path / "in",
        final_dir=tmp_path / "out",
        filename=FILENAME,
        client=_client(_s3()),
        now=parse_presign_window(PRESIGNED).signed_at,
    )

    assert destino == tmp_path / "out" / FILENAME
    assert destino.read_bytes() == PAYLOAD
    assert list((tmp_path / "in").iterdir()) == []


def test_reanuda_desde_el_part_existente(tmp_path):
    entrada = tmp_path / "in"
    entrada.mkdir()
    (entrada / (FILENAME + ".part")).write_bytes(PAYLOAD[:400])

    destino = download(
        PRESIGNED,
        incoming_dir=entrada,
        final_dir=tmp_path / "out",
        filename=FILENAME,
        client=_client(_s3()),
        now=parse_presign_window(PRESIGNED).signed_at,
    )

    assert destino.read_bytes() == PAYLOAD
    assert not (entrada / (FILENAME + ".part")).exists()


def test_si_el_servidor_ignora_el_range_se_reinicia(tmp_path):
    entrada = tmp_path / "in"
    entrada.mkdir()
    # Un `.part` con basura: si se concatenara, el resultado no sería PAYLOAD.
    (entrada / (FILENAME + ".part")).write_bytes(b"BASURA-BASURA")

    destino = download(
        PRESIGNED,
        incoming_dir=entrada,
        final_dir=tmp_path / "out",
        filename=FILENAME,
        client=_client(_s3(honor_range=False)),
        now=parse_presign_window(PRESIGNED).signed_at,
    )

    assert destino.read_bytes() == PAYLOAD


def test_un_part_ya_completo_se_verifica_sin_rebajar(tmp_path):
    # Corte entre la última escritura y el `os.replace`: S3 responde 416 y no
    # hay nada que bajar. Rebajar 7,5 GB por esto sería absurdo.
    entrada = tmp_path / "in"
    entrada.mkdir()
    (entrada / (FILENAME + ".part")).write_bytes(PAYLOAD)

    destino = download(
        PRESIGNED,
        incoming_dir=entrada,
        final_dir=tmp_path / "out",
        filename=FILENAME,
        client=_client(_s3()),
        now=parse_presign_window(PRESIGNED).signed_at,
    )

    assert destino.read_bytes() == PAYLOAD


def test_un_range_que_arranca_en_otro_byte_aborta(tmp_path):
    # Sin esta guarda, un servidor que devuelve otro offset concatenaría bytes
    # equivocados. Solo el checksum lo detectaría, y puede no venir.
    entrada = tmp_path / "in"
    entrada.mkdir()
    (entrada / (FILENAME + ".part")).write_bytes(PAYLOAD[:400])

    def handler(request: httpx.Request) -> httpx.Response:
        rango = request.headers.get("range")
        if rango:
            # Miente: dice devolver desde 0 aunque se pidió desde 400.
            return httpx.Response(
                206,
                content=PAYLOAD,
                headers={"content-range": f"bytes 0-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"},
            )
        return httpx.Response(200, content=PAYLOAD)

    with pytest.raises(DownloadError) as exc:
        download(
            PRESIGNED,
            incoming_dir=entrada,
            final_dir=tmp_path / "out",
            filename=FILENAME,
            client=_client(handler),
            now=parse_presign_window(PRESIGNED).signed_at,
        )

    assert not (tmp_path / "out" / FILENAME).exists()
    assert "corrupción" in str(exc.value)


def test_checksum_que_no_cierra_no_llega_al_destino_final(tmp_path):
    entrada = tmp_path / "in"

    with pytest.raises(DownloadIntegrityError) as exc:
        download(
            PRESIGNED,
            incoming_dir=entrada,
            final_dir=tmp_path / "out",
            filename=FILENAME,
            client=_client(_s3(checksum="AAAAAAAAAAAAAAAAAAAAAA==")),
            now=parse_presign_window(PRESIGNED).signed_at,
        )

    assert not (tmp_path / "out" / FILENAME).exists()
    assert (entrada / (FILENAME + ".part")).exists()
    assert "checksum" in str(exc.value).lower()
    assert "X-Amz" not in str(exc.value)


def test_sin_header_de_checksum_solo_valida_tamano(tmp_path):
    destino = download(
        PRESIGNED,
        incoming_dir=tmp_path / "in",
        final_dir=tmp_path / "out",
        filename=FILENAME,
        client=_client(_s3(checksum=None)),
        now=parse_presign_window(PRESIGNED).signed_at,
    )

    assert destino.read_bytes() == PAYLOAD


# --- S8: disco ---------------------------------------------------------------


def test_sin_espacio_suficiente_no_descarga(tmp_path):
    with pytest.raises(InsufficientSpaceError) as exc:
        download(
            PRESIGNED,
            incoming_dir=tmp_path / "in",
            final_dir=tmp_path / "out",
            filename=FILENAME,
            client=_client(_s3()),
            now=parse_presign_window(PRESIGNED).signed_at,
            free_bytes=10,
        )

    assert "espacio" in str(exc.value).lower()
    assert not (tmp_path / "out" / FILENAME).exists()


# --- errores de transporte ---------------------------------------------------


def test_un_403_significa_link_vencido_o_firma_invalida(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"<Error><Code>AccessDenied</Code></Error>")

    with pytest.raises(ExpiredLinkError) as exc:
        download(
            PRESIGNED,
            incoming_dir=tmp_path / "in",
            final_dir=tmp_path / "out",
            filename=FILENAME,
            client=_client(handler),
            now=parse_presign_window(PRESIGNED).signed_at,
        )

    assert "X-Amz" not in str(exc.value)


def test_un_404_es_error_de_descarga(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(DownloadError):
        download(
            PRESIGNED,
            incoming_dir=tmp_path / "in",
            final_dir=tmp_path / "out",
            filename=FILENAME,
            client=_client(handler),
            now=parse_presign_window(PRESIGNED).signed_at,
        )
