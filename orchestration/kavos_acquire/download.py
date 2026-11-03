"""Specs S2, S4, S5 y S8: descarga reanudable, integridad y commit atómico.

Contexto que manda el diseño: el artefacto pesa ~7,5 GB y la firma del link
presignado vive **12 horas** (`X-Amz-Expires=43200`). Con una conexión lenta eso
es un margen real, no teórico, y un corte de red a mitad de camino no se
arregla con reintentos si la firma ya venció: S3 valida la firma al iniciar el
request y responde 403.

Decisiones de implementación:

- **La descarga corre en proceso, no como subproceso.** `flow.py::_run` loguea
  `shlex.join(cmd)` y `ps` expone `argv`; cualquiera de los dos filtraría la
  credencial STS de la URL. Acá la URL solo viaja como argumento de `httpx`.
- **`follow_redirects=False`.** Seguir un `Location` mandaría `X-Amz-Signature`
  a un host que no elegimos.
- **El checksum se calcula leyendo el archivo terminado.** Un `.part`
  reanudado tiene bytes escritos por corridas anteriores, así que un CRC
  incremental no representa el archivo completo. Leer 7,5 GB locales es barato
  comparado con cargar datos sin verificar.
- **El commit es un `os.replace`** desde `.incoming/` a `mssql-backup/`, en el
  mismo sistema de archivos, así que es atómico: `bak-extract` nunca ve un
  archivo a medio escribir.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

# Margen mínimo de ventana para arrancar. Con 7,5 GB, empezar con minutos por
# delante es empezar para perder.
DEFAULT_MIN_WINDOW_SECONDS = 1800

# Tamaño de chunk: 1 MiB. Suficiente para no ahogar el disco en escrituras
# chicas, y chico para reportar progreso.
CHUNK_SIZE = 1024 * 1024

_REQUEST_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=300.0, pool=30.0)


class DownloadError(RuntimeError):
    """Falló la adquisición del artefacto."""


class ExpiredLinkError(DownloadError):
    """La firma del link venció o no fue aceptada. Reintentar no sirve."""


class DownloadIntegrityError(DownloadError):
    """Los bytes descargados no verifican contra el origen."""


class InsufficientSpaceError(DownloadError):
    """No hay espacio libre para completar la descarga."""


@dataclass(frozen=True)
class PresignWindow:
    """Ventana de validez declarada por la propia URL presignada."""

    signed_at: datetime
    expires_in: int

    @property
    def expires_at(self) -> datetime:
        return self.signed_at + timedelta(seconds=self.expires_in)

    def remaining(self, now: datetime) -> timedelta:
        return self.expires_at - _as_utc(now)


def _as_utc(momento: datetime) -> datetime:
    """Normaliza a UTC. Un datetime naive se interpreta como UTC, nunca como
    hora local del host: la firma está en UTC por definición."""
    if momento.tzinfo is None:
        return momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(timezone.utc)


def sanitize_url(url: str) -> str:
    """Devuelve la URL sin query ni fragmento, apta para logs.

    Spec S5: la query contiene `X-Amz-Security-Token` y `X-Amz-Signature`. Esta
    función es la única forma en que una URL puede aparecer en un mensaje.
    """
    partes = urlsplit(url)
    host = partes.hostname or ""
    puerto = f":{partes.port}" if partes.port else ""
    return urlunsplit((partes.scheme, f"{host}{puerto}", partes.path, "", ""))


def parse_presign_window(url: str) -> PresignWindow | None:
    """Extrae `X-Amz-Date` y `X-Amz-Expires` de la URL, si están."""
    query = parse_qs(urlsplit(url).query)
    crudo_fecha = (query.get("X-Amz-Date") or [""])[0]
    crudo_expira = (query.get("X-Amz-Expires") or [""])[0]
    if not crudo_fecha or not crudo_expira:
        return None
    try:
        firmado = datetime.strptime(crudo_fecha, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        expira = int(crudo_expira)
    except ValueError as exc:
        raise DownloadError(
            f"ventana de firma ilegible en {sanitize_url(url)} ({exc})"
        ) from exc
    if expira <= 0:
        raise DownloadError(f"ventana de firma inválida en {sanitize_url(url)}")
    return PresignWindow(signed_at=firmado, expires_in=expira)


def guard_window(
    window: PresignWindow | None,
    *,
    now: datetime | None = None,
    min_remaining_seconds: int = DEFAULT_MIN_WINDOW_SECONDS,
) -> None:
    """Aborta antes de tocar la red si la ventana no alcanza.

    Un link vencido no se arregla reintentando: hay que pedir uno nuevo. Fallar
    acá convierte eso en un mensaje accionable en vez de un 403 opaco a mitad
    de la descarga.
    """
    if window is None:
        return
    ahora = _as_utc(now or datetime.now(timezone.utc))
    restante = window.remaining(ahora)
    if restante < timedelta(seconds=min_remaining_seconds):
        minutos = restante.total_seconds() / 60
        if restante.total_seconds() <= 0:
            detalle = f"venció hace {abs(minutos):.0f} min"
        else:
            detalle = f"quedan {minutos:.0f} min y se exigen {min_remaining_seconds // 60}"
        raise ExpiredLinkError(
            f"ventana de la firma insuficiente ({detalle}). "
            f"Se requiere un link nuevo: la firma no se puede extender desde acá."
        )


def crc32_as_s3(data: bytes) -> str:
    """CRC32 en el formato que usa S3: base64 de 4 bytes big-endian."""
    return base64.b64encode(struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF)).decode()


def crc32_as_s3_of_file(path: Path) -> str:
    """CRC32 de un archivo, en streaming (no lo carga en memoria)."""
    acumulado = 0
    with open(path, "rb") as fh:
        while bloque := fh.read(CHUNK_SIZE):
            acumulado = zlib.crc32(bloque, acumulado)
    return base64.b64encode(struct.pack(">I", acumulado & 0xFFFFFFFF)).decode()


def _raise_for_status(status: int, url: str) -> None:
    if status in (200, 206, 416):
        return
    if status == 403:
        raise ExpiredLinkError(
            f"el origen rechazó la firma (HTTP 403) en {sanitize_url(url)}. "
            f"Link vencido o credencial inválida: se requiere un link nuevo."
        )
    if status == 404:
        raise DownloadError(
            f"el objeto ya no existe en {sanitize_url(url)} (HTTP 404)"
        )
    raise DownloadError(f"respuesta inesperada HTTP {status} de {sanitize_url(url)}")


def _total_from_headers(headers: httpx.Headers) -> int | None:
    rango = headers.get("content-range")
    if rango and "/" in rango:
        crudo = rango.rsplit("/", 1)[-1].strip()
        if crudo.isdigit():
            return int(crudo)
    longitud = headers.get("content-length")
    if longitud and longitud.isdigit():
        return int(longitud)
    return None


def _range_start(headers: httpx.Headers) -> int | None:
    """Byte inicial que el servidor dice estar devolviendo (`Content-Range`)."""
    rango = headers.get("content-range") or ""
    if not rango.startswith("bytes "):
        return None
    crudo = rango.removeprefix("bytes ").split("-", 1)[0].strip()
    return int(crudo) if crudo.isdigit() else None


def probe_total(url: str, client: httpx.Client) -> int | None:
    """Pregunta el tamaño total con `Range: bytes=0-0`, sin bajar el cuerpo.

    Se usa `stream` a propósito: con un servidor que ignore `Range`, un `get`
    normal intentaría traer los 7,5 GB a memoria.
    """
    with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as resp:
        _raise_for_status(resp.status_code, url)
        if resp.status_code == 416:
            return 0
        return _total_from_headers(resp.headers)


def _free_space(path: Path) -> int:
    destino = path
    while not destino.exists() and destino != destino.parent:
        destino = destino.parent
    return shutil.disk_usage(destino).free


def download(
    url: str,
    *,
    incoming_dir: str | Path,
    final_dir: str | Path,
    filename: str,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    min_window_seconds: int = DEFAULT_MIN_WINDOW_SECONDS,
    free_bytes: int | None = None,
) -> Path:
    """Descarga `url` a `incoming_dir/filename.part` y la comitea a `final_dir`.

    Reanuda desde el `.part` existente con `Range`, verifica checksum y tamaño, y
    solo entonces mueve el archivo a `final_dir` con `os.replace`.
    """
    entrada = Path(incoming_dir)
    salida = Path(final_dir)

    ventana = parse_presign_window(url)
    guard_window(ventana, now=now, min_remaining_seconds=min_window_seconds)

    entrada.mkdir(parents=True, exist_ok=True)
    salida.mkdir(parents=True, exist_ok=True)
    parcial = entrada / (filename + ".part")
    final = salida / filename

    propio = client is None
    http = client or httpx.Client(
        timeout=_REQUEST_TIMEOUT, follow_redirects=False
    )
    try:
        total = probe_total(url, http)

        ya_descargado = parcial.stat().st_size if parcial.exists() else 0
        if total is not None:
            faltante = max(total - ya_descargado, 0)
            disponible = free_bytes if free_bytes is not None else _free_space(entrada)
            if disponible < faltante:
                raise InsufficientSpaceError(
                    f"espacio insuficiente para {filename}: faltan "
                    f"{faltante / 1024**3:.2f} GiB por descargar y hay "
                    f"{disponible / 1024**3:.2f} GiB libres. "
                    f"Corré scripts/prune_backups.py antes de reintentar."
                )

        cabeceras = {"Range": f"bytes={ya_descargado}-"} if ya_descargado else {}
        with http.stream("GET", url, headers=cabeceras) as resp:
            _raise_for_status(resp.status_code, url)
            if resp.status_code == 416 and ya_descargado > 0:
                # El `.part` ya tiene todos los bytes (corte entre la última
                # escritura y el `os.replace`). S3 responde 416 con
                # `bytes */TOTAL`: no hay nada que bajar, solo verificar.
                logger.info(
                    "%s: el .part ya está completo (HTTP 416), se verifica", filename
                )
                esperado = _total_from_headers(resp.headers) or ya_descargado
                checksum_crc32 = resp.headers.get("x-amz-checksum-crc32")
            else:
                reanudando = ya_descargado > 0 and resp.status_code == 206
                if ya_descargado and not reanudando:
                    # El servidor ignoró el Range: el `.part` no sirve.
                    logger.warning(
                        "el origen ignoró el Range; se reinicia %s desde cero", filename
                    )
                    ya_descargado = 0
                elif reanudando:
                    inicio = _range_start(resp.headers)
                    if inicio is not None and inicio != ya_descargado:
                        # Sin esto, un servidor que devuelve otro offset
                        # produce un archivo corrupto que solo el checksum
                        # --si viene-- detectaría.
                        raise DownloadError(
                            f"{filename}: el origen devolvió el rango desde el byte "
                            f"{inicio} cuando se pidió desde {ya_descargado}. "
                            "Se descarta el .part para evitar corrupción silenciosa."
                        )
                modo = "ab" if reanudando else "wb"
                esperado = _total_from_headers(resp.headers)
                with open(parcial, modo) as fh:
                    for bloque in resp.iter_bytes(CHUNK_SIZE):
                        fh.write(bloque)
                checksum_crc32 = resp.headers.get("x-amz-checksum-crc32")

        tamanio = parcial.stat().st_size
        if esperado is not None and tamanio != esperado:
            raise DownloadIntegrityError(
                f"{filename}: se esperaban {esperado} bytes y hay {tamanio}. "
                f"El archivo queda en .incoming/ para reintentar el resume."
            )
        if checksum_crc32:
            real = crc32_as_s3_of_file(parcial)
            if real != checksum_crc32:
                raise DownloadIntegrityError(
                    f"{filename}: checksum CRC32 no coincide (origen "
                    f"{checksum_crc32}, descargado {real}). El archivo queda en "
                    f".incoming/ y no se publica."
                )
        else:
            logger.warning(
                "%s: el origen no envió checksum, solo se validó el tamaño", filename
            )

        os.replace(parcial, final)
        logger.info("descarga verificada y publicada: %s (%d bytes)", filename, tamanio)
        return final
    finally:
        if propio:
            http.close()
