#!/usr/bin/env python3
"""Mirror S3 <-> local para backup/restore del bucket Garage (PoC Data Lake).

OpenSpec `poc-data-lake-ducklake`, tareas 7.1 (spec `operations`: backup y
restore probados). Usado por `scripts/backup.sh` y `scripts/restore.sh`.

Usa `s3fs` del venv de bronze (`bronze/.venv`, ya fijado por dlt[s3]) porque
el host no tiene aws-cli/mc/rclone. Solo datos sintéticos del PoC.

Subcomandos:
    list s3://bucket/              lista objetos (conteo + bytes)
    download s3://bucket/ dir/    espeja bucket -> local (borra sobrantes)
    upload dir/ s3://bucket/      sube espejo local -> bucket (sin purgar)
    purge s3://bucket/            borra TODAS las claves (solo destroy test)

Env (mismos defaults que docker-compose.yml/.env.example):
    GARAGE_S3_KEY_ID, GARAGE_S3_SECRET, GARAGE_S3_ENDPOINT, GARAGE_S3_REGION
"""

from __future__ import annotations

import argparse
import os
import sys


def _fs():
    import s3fs  # del venv bronze (dlt[s3]); no es dep nueva

    garage_scheme = os.getenv("GARAGE_SCHEME", "http")
    garage_host = os.getenv("GARAGE_HOST", "localhost")
    garage_port = os.getenv("GARAGE_S3_PORT", "3900")
    return s3fs.S3FileSystem(
        key=os.getenv(
            "GARAGE_S3_KEY_ID", "GK0123456789abcdef0123456789abcdef"
        ),
        secret=os.getenv(
            "GARAGE_S3_SECRET",
            "0123456789abcdef0123456789abcdef"
            "0123456789abcdef0123456789abcdef",
        ),
        client_kwargs={
            "endpoint_url": os.getenv(
                "GARAGE_S3_ENDPOINT", f"{garage_scheme}://{garage_host}:{garage_port}"
            ),
            "region_name": os.getenv("GARAGE_S3_REGION", "garage"),
        },
        # Garage exige path-style (igual que el secret DuckDB `URL_STYLE path`).
        config_kwargs={"s3": {"addressing_style": "path"}},
    )


def _norm_s3(url: str) -> str:
    assert url.startswith("s3://"), f"se esperaba s3://..., llego {url!r}"
    return url.rstrip("/")


def cmd_list(fs, url: str) -> int:
    objs = fs.find(_norm_s3(url))
    total = sum(fs.info(o).get("size", 0) for o in objs)
    print(f"objetos: {len(objs)}, bytes: {total}")
    return 0


def cmd_download(fs, url: str, local: str) -> int:
    os.makedirs(local, exist_ok=True)
    # Limpia sobrantes locales para que el espejo sea exacto.
    for root, _dirs, files in os.walk(local):
        for f in files:
            os.remove(os.path.join(root, f))
    fs.get(f"{_norm_s3(url)}/", local, recursive=True)
    n = sum(len(files) for _, _, files in os.walk(local))
    print(f"download OK: {n} ficheros -> {local}")
    return 0


def cmd_upload(fs, local: str, url: str) -> int:
    n = 0
    for root, _dirs, files in os.walk(local):
        for f in files:
            src = os.path.join(root, f)
            rel = os.path.relpath(src, local)
            fs.put(src, f"{_norm_s3(url)}/{rel}")
            n += 1
    print(f"upload OK: {n} ficheros -> {url}")
    return 0


def cmd_purge(fs, url: str) -> int:
    prefix = _norm_s3(url)
    objs = fs.find(prefix)
    for o in objs:
        fs.rm(o)
    print(f"purge OK: {len(objs)} objetos borrados de {url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mirror S3<->local (Garage)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("url")
    p = sub.add_parser("download")
    p.add_argument("url")
    p.add_argument("local")
    p = sub.add_parser("upload")
    p.add_argument("local")
    p.add_argument("url")
    p = sub.add_parser("purge")
    p.add_argument("url")
    args = ap.parse_args(argv)
    fs = _fs()
    if args.cmd == "list":
        return cmd_list(fs, args.url)
    if args.cmd == "download":
        return cmd_download(fs, args.url, args.local)
    if args.cmd == "upload":
        return cmd_upload(fs, args.local, args.url)
    if args.cmd == "purge":
        confirm = os.getenv("KAVOS_LAKE_ALLOW_PURGE", "")
        if confirm != "yes":
            print(
                "rehusado: purge borra el bucket; exportar "
                "KAVOS_LAKE_ALLOW_PURGE=yes para confirmar",
                file=sys.stderr,
            )
            return 2
        return cmd_purge(fs, args.url)
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    sys.exit(main())
