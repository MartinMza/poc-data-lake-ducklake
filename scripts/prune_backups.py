#!/usr/bin/env python3
"""Poda `mssql-backup/` dejando un solo backup (spec S8, requisito L10).

Requisito del usuario, textual: "siempre sobre escribir y dejar un solo archivo
backup".

Este script es una **cáscara**: la lógica de qué se borra vive en
`kavos_acquire/retention.py` y la comparte el flow, que poda automáticamente
después de adquirir. Tener el algoritmo de borrado dos veces es cómo se pierde
un backup: la segunda copia se desincroniza y termina borrando lo que la primera
protegía.

El orden es por el timestamp del **nombre**, nunca por mtime: un `cp` o una
re-descarga cambia el mtime, y ordenar por él borraría el backup del día. Lo que
no parsea como backup fechado no se borra nunca (fixtures del repo incluidos) y
no se desciende a `.incoming/`, que tiene `.part` en vuelo.

Uso:
    orchestration/.venv/bin/python scripts/prune_backups.py                 # dry-run
    orchestration/.venv/bin/python scripts/prune_backups.py --apply         # borra
    orchestration/.venv/bin/python scripts/prune_backups.py --keep 3 --apply
    PRUNE_DIR=/otra/ruta orchestration/.venv/bin/python scripts/prune_backups.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "orchestration"),
)

from kavos_acquire.retention import (  # noqa: E402
    DEFAULT_KEEP,
    RetentionPlan,
    apply_retention,
    plan_retention,
)


def resolve_target_dir(explicit: str | None) -> Path:
    """El directorio a podar: argv manda, después `$PRUNE_DIR`, después el default."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("PRUNE_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "mssql-backup"


def _tamano(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _imprimir_plan(plan: RetentionPlan, objetivo: Path, keep: int) -> None:
    print(f"Plan de poda ({objetivo}, keep={keep}):")
    for identidad, path in plan.keep:
        print(
            f"  CONSERVAR  {path.name}  "
            f"({_tamano(path)} bytes, {identidad.stamp:%Y-%m-%d %H:%M:%S})"
        )
    for identidad, path in plan.delete:
        print(
            f"  BORRAR     {path.name}  "
            f"({_tamano(path)} bytes, {identidad.stamp:%Y-%m-%d %H:%M:%S})"
        )
    for path, motivo in plan.ignored:
        print(f"  IGNORADO   {path.name}  ({motivo})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Poda mssql-backup/ dejando los N backups más nuevos por NOMBRE "
            "(dry-run sin --apply)"
        )
    )
    parser.add_argument(
        "directorio",
        nargs="?",
        default=None,
        help="directorio con los backups (default: mssql-backup/ del repo o $PRUNE_DIR)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"cuántos backups nuevos conservar (default: {DEFAULT_KEEP})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="ejecuta el borrado (sin este flag solo muestra el plan)",
    )
    args = parser.parse_args(argv)

    if args.keep < 1:
        parser.error("--keep debe ser >= 1 (hay que conservar al menos un backup)")

    objetivo = resolve_target_dir(args.directorio)
    if not objetivo.is_dir():
        print(f"Error: {objetivo} no existe o no es un directorio; nada borrado.")
        return 1

    plan = plan_retention(objetivo, keep=args.keep)

    if not plan.keep and not plan.ignored:
        print(f"Nada que podar: {objetivo} no tiene backups reconocibles.")
        return 0

    _imprimir_plan(plan, objetivo, args.keep)

    if not args.apply:
        print("\nDry-run: nada borrado. Re-ejecutá con --apply para aplicar.")
        print(
            f"Resumen: conservados={len(plan.keep)}, "
            f"a borrar={len(plan.delete)} ({plan.freed_bytes} bytes liberables), "
            f"ignorados={len(plan.ignored)}."
        )
        return 0

    borrados = apply_retention(plan)
    print("\nPoda aplicada.")
    print(
        f"Resumen: conservados={len(plan.keep)}, "
        f"borrados={len(borrados)} ({plan.freed_bytes} bytes liberados), "
        f"ignorados={len(plan.ignored)}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
