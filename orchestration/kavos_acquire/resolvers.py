"""Resolución del link por proveedor.

El caso real es S3 presignado: no hay nada que resolver, la URL se descarga tal
cual, y ese es el camino que hoy corre en producción.

La clasificación responde **cómo se obtienen los bytes**, no si el destino es
confiable. La confianza la decide la allowlist de `mailbox.host_allowed`, en una
sola capa. Duplicar la lista de hosts acá sería la receta para que las dos
listas se desincronicen: un host https que no es de los conocidos se trata como
descarga directa, y si no está en la allowlist nunca llega hasta acá.

WeTransfer queda explícitamente sin soporte. Su descarga depende de un endpoint
no documentado que ya cambió varias veces; un scraper sin posibilidad de
probarlo contra el servicio real es peor que un fallo claro que pida S3 o
SharePoint.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

from kavos_acquire.mailbox import safe_https_url

logger = logging.getLogger(__name__)

# Cuánto del cuerpo se mira para detectar el interstitial de Drive. El aviso de
# "no se puede escanear" es HTML de unos pocos KB.
_PEEK_BYTES = 64 * 1024

_FORM_ACTION_RE = re.compile(r"<form[^>]*\baction=\"([^\"]+)\"", re.IGNORECASE)
_INPUT_RE = re.compile(
    r"<input[^>]*\bname=\"([^\"]+)\"[^>]*\bvalue=\"([^\"]*)\"", re.IGNORECASE
)
_DRIVE_FILE_ID_RE = re.compile(r"/file/d/([A-Za-z0-9_-]+)")


class UnsupportedLinkHost(RuntimeError):
    """El link no se puede resolver o no se debe intentar."""


class ResolverKind(Enum):
    DIRECT = "direct"
    GDRIVE = "gdrive"
    ONEDRIVE = "onedrive"
    WETRANSFER = "wetransfer"


def classify(url: str) -> ResolverKind:
    """Decide la estrategia de resolución a partir del host."""
    partes = urlsplit(url)
    host = (partes.hostname or "").lower()
    if not safe_https_url(url):
        raise UnsupportedLinkHost(
            f"link inválido: se exige https con host y sin credenciales "
            f"embebidas ({host or url})"
        )
    if host in {"drive.google.com", "drive.usercontent.google.com", "docs.google.com"}:
        return ResolverKind.GDRIVE
    if host == "we.tl" or host == "wetransfer.com" or host.endswith(".wetransfer.com"):
        return ResolverKind.WETRANSFER
    if (
        host.endswith(".sharepoint.com")
        or host == "onedrive.live.com"
        or host == "1drv.ms"
    ):
        return ResolverKind.ONEDRIVE
    return ResolverKind.DIRECT


def _with_param(url: str, name: str, value: str) -> str:
    partes = urlsplit(url)
    query = parse_qs(partes.query, keep_blank_values=True)
    query[name] = [value]
    return urlunsplit(
        (partes.scheme, partes.netloc, partes.path, urlencode(query, doseq=True), partes.fragment)
    )


def _has_param(url: str, name: str) -> bool:
    return name in parse_qs(urlsplit(url).query)


def _drive_file_id(url: str) -> str:
    partes = urlsplit(url)
    en_query = parse_qs(partes.query).get("id")
    if en_query and en_query[0]:
        return en_query[0]
    encontrado = _DRIVE_FILE_ID_RE.search(partes.path)
    if encontrado:
        return encontrado.group(1)
    raise UnsupportedLinkHost(
        "no se pudo extraer el id de archivo del link de Google Drive"
    )


def _resolve_gdrive(url: str, client: httpx.Client) -> str:
    """Convierte un link de vista en una descarga, resolviendo el interstitial.

    Drive intercepta los archivos grandes con una página de "no se puede
    escanear en busca de virus" que trae un formulario con `confirm` y `uuid`.
    Sin ese segundo paso se descarga HTML y el `.bak` resultante es basura.
    """
    identificador = _drive_file_id(url)
    directa = _with_param(
        f"https://drive.google.com/uc?export=download&id={identificador}",
        "id",
        identificador,
    )
    with client.stream("GET", directa) as resp:
        if resp.status_code >= 400:
            raise UnsupportedLinkHost(
                f"Google Drive respondió HTTP {resp.status_code} para el id "
                f"{identificador}"
            )
        tipo = (resp.headers.get("content-type") or "").lower()
        if "text/html" not in tipo:
            # Archivo chico: el propio `uc?export=download` ya entrega los bytes.
            return directa
        muestra = b""
        for bloque in resp.iter_bytes(8192):
            muestra += bloque
            if len(muestra) >= _PEEK_BYTES:
                break
    return _drive_confirm_url(muestra.decode("utf-8", errors="replace"), identificador)


def _drive_confirm_url(pagina: str, identificador: str) -> str:
    accion = _FORM_ACTION_RE.search(pagina)
    if accion is None:
        raise UnsupportedLinkHost(
            "Google Drive devolvió una página que no se pudo interpretar; "
            "no se descarga HTML como si fuera un backup"
        )
    parametros = dict(_INPUT_RE.findall(pagina))
    parametros.setdefault("id", identificador)
    return f"{accion.group(1)}?{urlencode(parametros)}"


def _resolve_onedrive(url: str) -> str:
    """`download=1` es lo que hace que SharePoint entregue el archivo."""
    return url if _has_param(url, "download") else _with_param(url, "download", "1")


def _reject_wetransfer() -> str:
    raise UnsupportedLinkHost(
        "WeTransfer no está soportado de forma automática: su descarga depende de "
        "un endpoint no documentado que cambia sin aviso, así que un scraper se "
        "rompería en silencio. Pedí el envío por S3 presignado o por una carpeta "
        "de SharePoint."
    )


def resolve(url: str, *, client: httpx.Client) -> str:
    """Devuelve la URL que hay que descargar para obtener los bytes."""
    tipo = classify(url)
    if tipo is ResolverKind.DIRECT:
        return url
    if tipo is ResolverKind.ONEDRIVE:
        return _resolve_onedrive(url)
    if tipo is ResolverKind.GDRIVE:
        return _resolve_gdrive(url, client)
    return _reject_wetransfer()
