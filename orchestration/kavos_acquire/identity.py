"""Spec S3: identidad del backup y guarda de monotonicidad.

El nombre del artefacto de Finnegans codifica la fecha del backup:

    FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz
    └── db ──┘ └──── fecha ────┘ └─ frac ─┘

`bronze-load` reemplaza tablas enteras, así que cargar un backup más viejo
retrocede el lago en el tiempo **sin ningún error**. `guard_monotonic` es la
única cosa que impide eso, y por eso falla cerrado: si el nombre no se puede
interpretar, no se avanza.

El timestamp del nombre es hora local del servidor de origen. Se compara contra
sí mismo (naive con naive) y nunca se asume UTC: inventar una zona sería peor
que no tenerla.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

# Sufijos válidos: el `.bak` suelto o comprimido dentro de un tar.
_COMPRESSION_SUFFIXES: tuple[str, ...] = (
    "",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tar.xz",
)

# `%Y_%m_%d_%H%M%S_<6 dígitos>`, con frontera numérica a ambos lados para no
# enganchar dígitos de un nombre de base que los preceda.
_STAMP_RE = re.compile(r"(?<!\d)(\d{4})_(\d{2})_(\d{2})_(\d{2})(\d{2})(\d{2})_(\d{6})(?!\d)")


class BackupNameError(ValueError):
    """El nombre del artefacto no identifica un backup interpretable."""


class StaleBackupError(ValueError):
    """El backup es más viejo (o el mismo) que el último aplicado con éxito."""


@dataclass(frozen=True, order=True)
class BackupId:
    """Identidad ordenable de un backup.

    Ordena por `(stamp, fraction, filename)`: la fecha del nombre manda y la
    fracción desempata dos backups dentro del mismo segundo.
    """

    stamp: datetime
    fraction: int
    filename: str

    def to_dict(self) -> dict[str, object]:
        return {
            "stamp": self.stamp.isoformat(),
            "fraction": self.fraction,
            "filename": self.filename,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> BackupId:
        return cls(
            stamp=datetime.fromisoformat(str(raw["stamp"])),
            fraction=int(str(raw["fraction"])),
            filename=str(raw["filename"]),
        )

    def describe(self) -> str:
        return f"{self.filename} ({self.stamp:%Y-%m-%d %H:%M:%S})"


def _basename(name: str) -> str:
    """Acepta tanto un nombre pelado como la ruta de una URL presignada."""
    return PurePosixPath(name.replace("\\", "/")).name


def _is_backup_filename(lower: str) -> bool:
    return any(lower.endswith(".bak" + suffix) for suffix in _COMPRESSION_SUFFIXES)


def parse_backup_id(name: str) -> BackupId:
    """Extrae la identidad del backup desde su nombre.

    Falla cerrado: un nombre sin fecha interpretable o con extensión que no es
    un backup levanta `BackupNameError` en vez de adivinar.
    """
    base = _basename(name)
    if not _is_backup_filename(base.lower()):
        raise BackupNameError(
            f"{base}: no es un backup reconocible (se espera .bak o "
            f".bak.{{tar,tar.gz,tgz,tar.bz2,tar.xz}})"
        )
    match = _STAMP_RE.search(base)
    if match is None:
        raise BackupNameError(
            f"{base}: no se pudo leer la fecha del backup "
            f"(se espera <db>_<YYYY>_<MM>_<DD>_<HHMMSS>_<6 dígitos>.bak)"
        )
    year, month, day, hour, minute, second, fraction = (int(g) for g in match.groups())
    try:
        stamp = datetime(year, month, day, hour, minute, second)
    except ValueError as exc:
        raise BackupNameError(f"{base}: fecha imposible en el nombre ({exc})") from exc
    return BackupId(stamp=stamp, fraction=fraction, filename=base)


def guard_monotonic(candidate: BackupId, last_applied: BackupId | None) -> None:
    """Aborta si `candidate` no es estrictamente más nuevo que `last_applied`.

    Es una función pura: no escribe estado. El llamador persiste el nuevo
    backup solo después de que `bronze-load` haya tenido éxito, así que un
    rechazo acá no deja rastro.
    """
    if last_applied is None:
        return
    if candidate <= last_applied:
        raise StaleBackupError(
            f"backup descartado por antigüedad: {candidate.describe()} no es más "
            f"nuevo que el último aplicado {last_applied.describe()}. "
            f"Cargarlo retrocedería el lago; se requiere un artefacto posterior."
        )
