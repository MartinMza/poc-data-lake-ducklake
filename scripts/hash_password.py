#!/usr/bin/env python3
"""Genera el hash PBKDF2 de un password para `AUTH_USERS`.

Uso:
    .venv/bin/python scripts/hash_password.py            # pide el password
    echo 'mi-password' | .venv/bin/python scripts/hash_password.py --password-stdin

El password nunca se pasa por argumento: quedaría en el `history` del shell y
visible en `ps` para otros usuarios del host.

El valor impreso se pega en `.env` como:
    AUTH_USERS="admin:pbkdf2_sha256$<iteraciones>$<salt>$<hash>"
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kavos_lake.infrastructure.security import hash_password  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hash PBKDF2 para AUTH_USERS")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="lee el password de stdin (para automatización, evita argv)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="iteraciones PBKDF2 (default: el del módulo de auth)",
    )
    args = parser.parse_args(argv)

    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repetir password: "):
            print("Los passwords no coinciden.", file=sys.stderr)
            return 1
    if not password:
        print("El password no puede estar vacío.", file=sys.stderr)
        return 1

    kwargs = {"iterations": args.iterations} if args.iterations else {}
    print(hash_password(password, **kwargs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
