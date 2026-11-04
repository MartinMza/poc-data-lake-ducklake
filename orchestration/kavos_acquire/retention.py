"""Spec S8 (L10): retención de exactamente un backup.

Requisito del usuario, textual: "siempre sobre escribir y dejar un solo archivo
backup". La lógica vive en un solo lugar y la comparten los dos consumidores:

- el orquestador (`pipeline`), que poda automáticamente después de adquirir;
- `scripts/prune_backups.py`, que es el CLI para correrlo a mano.

Dos implementaciones de borrado es cómo se pierde un backup: la segunda se
desincroniza y termina borrando lo que la primera protegía.

Reglas, en orden de importancia:

1. El orden es por el timestamp del **nombre**, nunca por mtime. Un `cp` o una
   re-descarga cambia el mtime y ordenar por él borraría el backup del día.
2. Lo que no parsea como backup fechado no se borra **nunca**. En
   `mssql-backup/` conviven fixtures (`test-source.bak`, `bak2pq-synth.bak`) que
   no son artefactos de producción.
3. No se desciende a subdirectorios: `.incoming/` tiene `.part` en vuelo.
4. `keep < 1` se coacciona a 1. Un `keep` mal pasado no puede dejar el
   directorio sin ningún backup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from kavos_acquire.identity import BackupId, BackupNameError, parse_backup_id

logger = logging.getLogger(__name__)

# Requisito L10: un solo archivo.
DEFAULT_KEEP = 1


@dataclass(frozen=True)
class RetentionPlan:
    """Clasificación de un directorio de backups, sin efectos todavía."""

    keep: tuple[tuple[BackupId, Path], ...] = ()
    delete: tuple[tuple[BackupId, Path], ...] = ()
    ignored: tuple[tuple[Path, str], ...] = ()
    # Se captura al planificar, no se lee del disco después: el resumen del CLI
    # se imprime luego de aplicar, y ahí los archivos ya no existen.
    freed_bytes: int = 0

    def describe(self) -> str:
        return (
            f"conservados={len(self.keep)}, a borrar={len(self.delete)} "
            f"({self.freed_bytes} bytes liberables), ignorados={len(self.ignored)}"
        )


def plan_retention(directory: str | Path, keep: int = DEFAULT_KEEP) -> RetentionPlan:
    """Clasifica el contenido de `directory` en conservados, a borrar e ignorados."""
    base = Path(directory)
    if not base.is_dir():
        return RetentionPlan()
    efectivo = max(int(keep), 1)

    candidatos: list[tuple[BackupId, Path]] = []
    ignorados: list[tuple[Path, str]] = []
    for entrada in sorted(base.iterdir(), key=lambda p: p.name):
        if entrada.is_symlink() or entrada.is_dir():
            continue
        if not entrada.is_file():
            continue
        try:
            candidatos.append((parse_backup_id(entrada.name), entrada))
        except BackupNameError as exc:
            ignorados.append((entrada, str(exc)))

    ordenados = sorted(candidatos, key=lambda item: item[0], reverse=True)
    a_borrar = ordenados[efectivo:]
    liberados = 0
    for _, ruta in a_borrar:
        try:
            liberados += ruta.stat().st_size
        except OSError:
            continue
    return RetentionPlan(
        keep=tuple(ordenados[:efectivo]),
        delete=tuple(a_borrar),
        ignored=tuple(ignorados),
        freed_bytes=liberados,
    )


def apply_retention(plan: RetentionPlan) -> list[Path]:
    """Borra lo que el plan indica y devuelve los paths efectivamente borrados.

    Un fallo de borrado se registra y no aborta: el backup nuevo ya está
    verificado y commiteado, así que la adquisición es exitosa aunque quede
    basura para limpiar a mano.
    """
    borrados: list[Path] = []
    for identidad, ruta in plan.delete:
        try:
            ruta.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("retención: no se pudo borrar %s (%s)", ruta.name, exc)
            continue
        logger.info("retención: borrado %s", identidad.describe())
        borrados.append(ruta)
    return borrados
