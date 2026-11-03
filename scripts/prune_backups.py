#!/usr/bin/env python3
"""Poda de backups en `mssql-backup/`: retiene los N más nuevos (Spec S8).

Contexto: cada backup pesa ~7,5 GB y llegan 1–2 por semana (~60 GB/mes), así
que el crecimiento sin techo hay que podarlo a mano con una política
declarada. El orden lo da la fecha del NOMBRE
(`kavos_acquire.identity.parse_backup_id`), nunca el mtime: un `cp` o una
re-descarga cambian el mtime sin cambiar qué backup es más nuevo, y ordenar
por mtime borraría el backup equivocado.

Qué hace:
- solo mira archivos regulares en el tope del directorio (nunca desciende a
  subdirectorios: `.incoming/` con sus `.part` en vuelo queda intacto);
- solo considera nombres que parsean como backup (`.bak`, `.bak.tar.gz`,
  `.tgz`, `.tar`, `.tar.bz2`, `.tar.xz` con timestamp); lo demás NUNCA se
  borra y se reporta como ignorado;
- conserva los N más nuevos y propone borrar el excedente;
- nunca borra el backup más nuevo: si algo quedó mal configurado, antes de
  dejar el directorio sin backups conserva todo.

Uso:
    orchestration/.venv/bin/python scripts/prune_backups.py                 # dry-run
    orchestration/.venv/bin/python scripts/prune_backups.py --apply         # borra
    orchestration/.venv/bin/python scripts/prune_backups.py --keep 3 --apply
    PRUNE_DIR=/otra/ruta orchestration/.venv/bin/python scripts/prune_backups.py

Sin `--apply` solo muestra el plan (misma convención que purge_sample_data.py).
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

from kavos_acquire.identity import BackupId, BackupNameError, parse_backup_id  # noqa: E402

DEFAULT_KEEP = 2


def resolve_target_dir(explicit: str | None) -> Path:
    """El directorio a podar: argv manda, después `$PRUNE_DIR`, después el default."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("PRUNE_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "mssql-backup"


def build_plan(
    backup_dir: Path, keep: int
) -> tuple[list[tuple[BackupId, Path]], list[tuple[BackupId, Path]], list[tuple[Path, str]]]:
    """Clasifica el tope de `backup_dir` en (conservados, a_borrar, ignorados).

    El orden es por identidad del NOMBRE (`BackupId`: fecha, fracción, nombre),
    nunca por mtime. Los conservados son los `keep` más nuevos. Lo que no
    parsea como backup cae en ignorados y jamás se borra. Como última defensa
    (Spec S8.7), el backup más nuevo nunca integra `a_borrar`: si `keep` llega
    mal, se conserva todo antes que dejar el directorio sin backups.
    """
    candidatos: list[tuple[BackupId, Path]] = []
    ignorados: list[tuple[Path, str]] = []
    for entrada in sorted(backup_dir.iterdir(), key=lambda p: p.name):
        if entrada.is_symlink() or entrada.is_dir():
            continue  # Nunca descender: .incoming/ y sus .part quedan intactos.
        if not entrada.is_file():
            continue
        try:
            identidad = parse_backup_id(entrada.name)
        except BackupNameError as exc:
            ignorados.append((entrada, str(exc)))
            continue
        candidatos.append((identidad, entrada))

    ordenados = sorted(candidatos, key=lambda item: item[0], reverse=True)
    if keep < 1:
        keep = 1  # El CLI ya rechaza N < 1; acá se conserva todo antes que vaciar.
    conservados = ordenados[:keep]
    a_borrar = ordenados[keep:]
    if ordenados and ordenados[0] in a_borrar:
        conservados, a_borrar = ordenados, []  # No debería pasar; fallar conservando.
    return conservados, a_borrar, ignorados


def _tamano(path: Path) -> int:
    return path.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poda mssql-backup/ conservando los N backups más nuevos por NOMBRE (dry-run sin --apply)"
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

    conservados, a_borrar, ignorados = build_plan(objetivo, args.keep)

    if not conservados and not ignorados:
        print(f"Nada que podar: {objetivo} no tiene backups reconocibles.")
        return 0

    print(f"Plan de poda ({objetivo}, keep={args.keep}):")
    for identidad, path in conservados:
        print(f"  CONSERVAR  {path.name}  ({_tamano(path)} bytes, {identidad.stamp:%Y-%m-%d %H:%M:%S})")
    for identidad, path in a_borrar:
        print(f"  BORRAR     {path.name}  ({_tamano(path)} bytes, {identidad.stamp:%Y-%m-%d %H:%M:%S})")
    for path, motivo in ignorados:
        print(f"  IGNORADO   {path.name}  ({motivo})")

    liberables = sum(_tamano(path) for _, path in a_borrar)
    if not args.apply:
        print("\nDry-run: nada borrado. Re-ejecutá con --apply para aplicar.")
        print(
            f"Resumen: conservados={len(conservados)}, "
            f"a borrar={len(a_borrar)} ({liberables} bytes liberables), "
            f"ignorados={len(ignorados)}."
        )
        return 0

    borrados = 0
    liberados = 0
    for _, path in a_borrar:
        liberados += _tamano(path)
        path.unlink()
        borrados += 1

    print("\nPoda aplicada.")
    print(
        f"Resumen: conservados={len(conservados)}, "
        f"borrados={borrados} ({liberados} bytes liberados), "
        f"ignorados={len(ignorados)}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
