"""Spec S5: estado persistido entre corridas.

Guarda lo mínimo para no reprocesar trabajo y para que la guarda de S3 tenga
memoria: el UID más alto visto, los `Message-ID` ya procesados y el último
backup aplicado con éxito. **Nunca** guarda el link de descarga: la URL
presignada es una credencial STS viva y no puede tocar el disco.

Un estado ilegible levanta `StateError` en vez de degradar a vacío. Degradar
sería peor: `applied == None` desactiva la guarda de monotonicidad y deja pasar
un artefacto viejo, que es exactamente el fallo que S3 previene.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from kavos_acquire.identity import BackupId

# Tope de `Message-ID` recordados. Acotado para que el archivo no crezca sin
# techo; 500 cubre con holgura un año a dos correos por semana.
MAX_PROCESSED_MESSAGE_IDS = 500

_STATE_VERSION = 1


class StateError(RuntimeError):
    """El estado persistido no se puede leer o no tiene forma válida."""


@dataclass
class AcquireState:
    """Estado del poller. Orden de campos = orden de serialización."""

    last_uid: int = 0
    processed_message_ids: list[str] = field(default_factory=list)
    applied: BackupId | None = None

    def is_processed(self, message_id: str) -> bool:
        return message_id in self.processed_message_ids

    def note_message(self, message_id: str) -> None:
        """Registra un mensaje como procesado, sin duplicar y acotado."""
        if not message_id or message_id in self.processed_message_ids:
            return
        self.processed_message_ids.append(message_id)
        exceso = len(self.processed_message_ids) - MAX_PROCESSED_MESSAGE_IDS
        if exceso > 0:
            del self.processed_message_ids[:exceso]

    def note_uid(self, uid: int) -> None:
        """El UID de IMAP es monótono: solo avanza."""
        if uid > self.last_uid:
            self.last_uid = uid

    def note_applied(self, backup: BackupId) -> None:
        """Marca el backup como aplicado. El llamador lo hace **después** de
        que la ingesta a Bronze tuvo éxito."""
        self.applied = backup

    def to_dict(self) -> dict[str, object]:
        return {
            "version": _STATE_VERSION,
            "last_uid": self.last_uid,
            "processed_message_ids": list(self.processed_message_ids),
            "applied": self.applied.to_dict() if self.applied is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> AcquireState:
        applied_raw = raw.get("applied")
        ids_raw = raw.get("processed_message_ids") or []
        if not isinstance(ids_raw, list):
            raise ValueError("processed_message_ids debe ser una lista")
        return cls(
            last_uid=int(raw.get("last_uid") or 0),
            processed_message_ids=[str(item) for item in ids_raw],
            applied=(
                BackupId.from_dict(applied_raw)
                if isinstance(applied_raw, dict)
                else None
            ),
        )


def load_state(path: str | Path) -> AcquireState:
    """Lee el estado. Archivo ausente es estado vacío; archivo roto es error."""
    ruta = Path(path)
    try:
        crudo = ruta.read_text(encoding="utf-8")
    except FileNotFoundError:
        return AcquireState()
    except OSError as exc:
        raise StateError(f"{ruta.name}: no se pudo leer el estado ({exc})") from exc

    try:
        raw = json.loads(crudo)
    except ValueError as exc:
        raise StateError(
            f"{ruta.name}: estado corrupto, no es JSON válido ({exc}). "
            f"Revisalo a mano: borrarlo desactiva la guarda de antigüedad."
        ) from exc

    if not isinstance(raw, dict):
        raise StateError(f"{ruta.name}: el estado debe ser un objeto JSON")

    try:
        return AcquireState.from_dict(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError(f"{ruta.name}: estado con forma inválida ({exc})") from exc


def save_state(path: str | Path, state: AcquireState) -> None:
    """Escribe el estado de forma atómica.

    Temporal en el mismo directorio + `os.replace`, que es atómico dentro del
    mismo sistema de archivos. Una corrida interrumpida deja el estado anterior
    intacto, nunca uno a medio escribir.
    """
    ruta = Path(path)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_name(ruta.name + ".tmp")
    payload = json.dumps(state.to_dict(), indent=1, sort_keys=True) + "\n"
    try:
        with open(temporal, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporal, ruta)
    except OSError as exc:
        temporal.unlink(missing_ok=True)
        raise StateError(f"{ruta.name}: no se pudo escribir el estado ({exc})") from exc
