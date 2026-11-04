"""Specs S1, S3 y S9: orquestador de la adquisición.

Compone buzón, allowlist, guarda de antigüedad y descarga. Es lo que consume
`flow.py`, y es el lugar donde se decide qué fallo se reintenta:

| Fallo | Estado avanza | Se reintenta |
|---|---|---|
| link vencido / firma rechazada | sí | no |
| host fuera de la allowlist | sí | no |
| artefacto más viejo que el aplicado | sí | no |
| error de red o de integridad | **no** | sí |

La razón de avanzar el estado en los fallos terminales: si no avanzara, la
primera corrida que se cruce con un link muerto dejaría el pipeline trabado en
ese mismo correo para siempre, y un backup nuevo que llegue después nunca se
procesaría. La razón de **no** avanzar en los transitorios: el `.part` ya
descargado se reanuda y el trabajo no se pierde.

El estado nunca marca `applied`: eso lo hace `mark_applied` después de que la
ingesta a Bronze tuvo éxito. Marcar antes convertiría un fallo de carga en un
backup que nunca se ingesta.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from kavos_acquire.download import (
    DEFAULT_MIN_WINDOW_SECONDS,
    DownloadError,
    ExpiredLinkError,
    download,
)
from kavos_acquire.identity import (
    BackupId,
    BackupNameError,
    StaleBackupError,
    guard_monotonic,
    parse_backup_id,
)
from kavos_acquire.mailbox import (
    LinkCandidate,
    connect_gmail,
    scan_mailbox,
)
from kavos_acquire.resolvers import UnsupportedLinkHost, resolve
from kavos_acquire.retention import DEFAULT_KEEP, apply_retention, plan_retention
from kavos_acquire.state import AcquireState, load_state, save_state

logger = logging.getLogger(__name__)

STATUS_ACQUIRED = "acquired"
STATUS_NOTHING = "no_new_backup"

# Ventana de búsqueda en el buzón (requisito L10: filtrar por fecha). Menor que
# la firma de 12 h: un aviso más viejo ya no se puede descargar.
DEFAULT_SINCE_DAYS = 3


class AcquisitionFailed(RuntimeError):
    """La corrida no pudo adquirir nada y hay que mirarla."""


@dataclass(frozen=True)
class AcquireOutcome:
    status: str
    path: Path | None = None
    backup_id: BackupId | None = None
    message: str = ""


def _filename_of(url: str) -> str:
    return urlsplit(url).path.rsplit("/", 1)[-1]


def _agrupar_por_uid(candidatos: list[LinkCandidate]) -> dict[int, list[LinkCandidate]]:
    agrupados: dict[int, list[LinkCandidate]] = {}
    for candidato in candidatos:
        agrupados.setdefault(candidato.uid, []).append(candidato)
    return agrupados


def _elegir(candidatos: list[LinkCandidate]) -> tuple[LinkCandidate | None, BackupId | None, str]:
    """Elige el candidato más nuevo del grupo, con su identidad ya parseada."""
    mejor: tuple[LinkCandidate, BackupId] | None = None
    problemas: list[str] = []
    for candidato in candidatos:
        if not candidato.allowed:
            problemas.append(f"uid {candidato.uid}: {candidato.reason}")
            continue
        try:
            backup = parse_backup_id(_filename_of(candidato.url))
        except BackupNameError as exc:
            problemas.append(f"uid {candidato.uid}: {exc}")
            continue
        if mejor is None or backup > mejor[1]:
            mejor = (candidato, backup)
    if mejor is None:
        return None, None, "; ".join(problemas)
    return mejor[0], mejor[1], "; ".join(problemas)


def _podar(directorio: str | Path, keep: int) -> list[Path]:
    """Spec S8 (L10): deja un solo backup en el directorio final.

    Se llama **después** de que el artefacto nuevo verificó checksum y quedó
    commiteado. Borrar antes sería perder los dos si la descarga falla.
    """
    plan = plan_retention(directorio, keep=keep)
    if not plan.delete:
        return []
    logger.info("retención: %s", plan.describe())
    return apply_retention(plan)


def mark_applied(state_path: str | Path, backup: BackupId) -> None:
    """Registra el backup como efectivamente cargado en el lago.

    Lo llama el flow **después** de que `bronze-load` terminó bien.
    """
    estado = load_state(state_path)
    estado.note_applied(backup)
    save_state(state_path, estado)


def acquire_latest_bak(
    *,
    state_path: str | Path,
    incoming_dir: str | Path,
    final_dir: str | Path,
    imap_user: str = "",
    imap_password: str = "",
    sender: str,
    subject_pattern: str,
    link_hosts: list[str],
    imap_client=None,
    http_client: httpx.Client | None = None,
    now: datetime | None = None,
    free_bytes: int | None = None,
    min_window_seconds: int = DEFAULT_MIN_WINDOW_SECONDS,
    since_days: int = DEFAULT_SINCE_DAYS,
    keep_backups: int = DEFAULT_KEEP,
) -> AcquireOutcome:
    """Busca el backup más nuevo disponible en el buzón y lo deja en `final_dir`."""
    estado = load_state(state_path)
    propio_imap = imap_client is None
    imap = imap_client or connect_gmail(imap_user, imap_password)
    propio_http = http_client is None
    http = http_client or httpx.Client(timeout=60.0, follow_redirects=False)
    fecha_desde = (now or datetime.now(timezone.utc)).date() - timedelta(days=since_days)

    try:
        # readonly: el poller no marca correos como leídos. La deduplicación la
        # lleva el estado por `Message-ID`, no las banderas del buzón.
        imap.select("INBOX", readonly=True)
        candidatos = scan_mailbox(
            imap,
            since_uid=estado.last_uid,
            since_date=fecha_desde,
            sender=sender,
            subject_pattern=subject_pattern,
            link_hosts=link_hosts,
        )
        if not candidatos:
            return AcquireOutcome(status=STATUS_NOTHING, message="sin correos nuevos")

        terminales: list[str] = []
        for uid in sorted(_agrupar_por_uid(candidatos), reverse=True):
            grupo = _agrupar_por_uid(candidatos)[uid]
            candidato, backup, problemas = _elegir(grupo)
            if problemas:
                terminales.append(problemas)
            if candidato is None or backup is None:
                _marcar_handled(state_path, estado, uid, grupo)
                continue

            try:
                guard_monotonic(backup, estado.applied)
            except StaleBackupError as exc:
                terminales.append(str(exc))
                _marcar_handled(state_path, estado, uid, grupo)
                continue

            try:
                url = resolve(candidato.url, client=http)
                destino = download(
                    url,
                    incoming_dir=incoming_dir,
                    final_dir=final_dir,
                    filename=backup.filename,
                    client=http,
                    now=now,
                    free_bytes=free_bytes,
                    min_window_seconds=min_window_seconds,
                )
            except UnsupportedLinkHost as exc:
                # Terminal: reintentar el mismo link no cambia nada.
                terminales.append(f"uid {uid}: {exc}")
                _marcar_handled(state_path, estado, uid, grupo)
                continue
            except ExpiredLinkError as exc:
                # Terminal: la firma no se puede extender desde acá. Reintentar
                # solo gasta corridas hasta que llegue un correo nuevo.
                terminales.append(f"uid {uid}: {exc}")
                _marcar_handled(state_path, estado, uid, grupo)
                continue
            except DownloadError as exc:
                # Transitorio (red, integridad, espacio): el estado NO avanza,
                # el `.part` queda y el próximo intento reanuda. Se unifica el
                # tipo de excepción para que el flow tenga un solo `except`.
                raise AcquisitionFailed(
                    f"fallo transitorio al adquirir {backup.filename}; se "
                    f"reintenta en la próxima corrida: {exc}"
                ) from exc
            else:
                _marcar_handled(state_path, estado, uid, grupo)
                podados = _podar(final_dir, keep_backups)
                detalle = f"adquirido {backup.filename}"
                if podados:
                    detalle += f" (borrados {len(podados)} anteriores)"
                return AcquireOutcome(
                    status=STATUS_ACQUIRED,
                    path=destino,
                    backup_id=backup,
                    message=detalle,
                )

        if terminales:
            raise AcquisitionFailed(
                "no se pudo adquirir ningún backup de los correos disponibles: "
                + " | ".join(terminales)
            )
        return AcquireOutcome(status=STATUS_NOTHING, message="sin backups nuevos")
    finally:
        if propio_imap:
            try:
                imap.logout()
            except Exception:  # noqa: BLE001 - cerrar nunca debe tapar el error real
                logger.debug("fallo al cerrar IMAP", exc_info=True)
        if propio_http:
            http.close()


def _marcar_handled(
    state_path: str | Path,
    estado: AcquireState,
    uid: int,
    grupo: list[LinkCandidate],
) -> None:
    """Da por procesado un correo, para no reintentarlo en la próxima corrida."""
    for candidato in grupo:
        estado.note_message(candidato.message_id)
    estado.note_uid(uid)
    save_state(state_path, estado)
