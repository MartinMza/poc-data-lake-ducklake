"""RSS propio del proceso: medición y presupuesto (T3-T7).

Reemplaza a `RLIMIT_AS` como guarda de memoria. `RLIMIT_AS` limita el
*espacio de direcciones* (virtual), no la memoria residente: las reservas de
`mmap` de Arrow/Rust y del driver ODBC agotan el espacio virtual mucho antes
de usar memoria real, y el proceso muere con SIGSEGV (exit 139) con RSS de
apenas ~90 MB. Eso no es una guarda, es una trampa.

La guarda real es doble: el cgroup del contenedor (techo duro) y, dentro del
proceso, medir RSS y abortar con un error limpio y explicable
(`MemoryBudgetExceeded`) en vez de un SIGSEGV o un OOM-kill.
"""

from __future__ import annotations

import os


class MemoryBudgetExceeded(RuntimeError):
    """El RSS propio superó (o exige superar) el presupuesto configurado.

    Error limpio: la corrida escribe `report.json`, imprime el resumen,
    elimina la base temporal y sale con exit != 0.
    """


def rss_mb() -> float:
    """RSS actual del proceso en MiB.

    Lee `/proc/self/statm` (residente × tamaño de página); si no existe
    (no-Linux), cae a `/proc/self/status` (`VmRSS`) y por último a
    `resource.getrusage` (pico, aproximado). Nunca lanza: si nada funciona
    devuelve 0.0 (medición no disponible, no presupuesto infinito).
    """
    try:
        with open("/proc/self/statm", encoding="utf-8") as fh:
            resident_pages = int(fh.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except Exception:  # noqa: BLE001 — cualquier fallback vale.
        pass
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except Exception:  # noqa: BLE001 — último recurso abajo.
        pass
    try:
        import resource

        # ru_maxrss es pico en KiB (Linux) o bytes (macOS): solo aproximado.
        peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return peak / 1024.0 if peak < 10**9 else peak / (1024 * 1024)
    except Exception:  # noqa: BLE001 — medición no disponible.
        return 0.0


def check_rss_budget(limit_mb: int, *, context: str) -> float:
    """Devuelve el RSS actual; si supera `limit_mb` aborta limpio (T3-T7).

    `limit_mb <= 0` desactiva la guarda (devuelve la medición sin comparar).
    """
    current = rss_mb()
    if limit_mb > 0 and current > limit_mb:
        raise MemoryBudgetExceeded(
            f"presupuesto de memoria agotado en {context}: RSS={current:.0f} MB "
            f"supera el límite de {limit_mb} MB (memory-limit-mb). La corrida "
            "se aborta antes de que el contenedor la mate (OOM/SIGSEGV): "
            "bajá el lote con --batch-size/--max-bytes-per-batch o subí el límite."
        )
    return current
