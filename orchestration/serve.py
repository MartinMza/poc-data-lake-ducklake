#!/usr/bin/env python3
"""Corre `kavos-lake-medallion` con schedule declarado (spec S7.3).

Sin cron externo improvisado: `flow.serve(...)` levanta el flow con un
schedule cron derivado de `ACQUIRE_POLL_HOURS` (default 4, la cadencia
estricta de S1 que cubre la ventana de 12 h del link).

Uso:
    orchestration/.venv/bin/python orchestration/serve.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flow import kavos_lake_medallion_flow

SERVE_NAME = "kavos-lake-medallion-scheduled"
DEFAULT_POLL_HOURS = 4


def cron_from_hours(hours: int) -> str:
    """Cron Prefect para correr cada `hours` horas.

    1 -> cada hora (`0 * * * *`), 24 -> diario a medianoche
    (`0 0 * * *`), el resto -> `0 */N * * *`. Solo acepta 1..24: la ventana
    del link es de 12 h y un poll más espaciado deja morir todos los links.
    """
    if hours <= 0 or hours > 24:
        raise ValueError(
            f"ACQUIRE_POLL_HOURS debe estar en 1..24, llegó {hours!r}")
    if hours == 1:
        return "0 * * * *"
    if hours == 24:
        return "0 0 * * *"
    return f"0 */{hours} * * *"


def main() -> None:
    """Registra el deployment y sirve el schedule (bloquea)."""
    poll_hours = int(os.getenv("ACQUIRE_POLL_HOURS", str(DEFAULT_POLL_HOURS)))
    kavos_lake_medallion_flow.serve(
        name=SERVE_NAME, cron=cron_from_hours(poll_hours))


if __name__ == "__main__":
    main()
