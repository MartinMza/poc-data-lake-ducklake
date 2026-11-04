"""Tests sin motor: medición RSS y presupuesto limpio (T3-T7, mem.py).

`RLIMIT_AS` limitaba direcciones, no memoria: el reemplazo mide RSS real y
aborta con `MemoryBudgetExceeded` (report + teardown + exit != 0).
"""

import pytest

from bak2parquet.mem import MemoryBudgetExceeded, check_rss_budget, rss_mb


def test_rss_mb_mide_residente_positivo():
    assert rss_mb() > 0.0


def test_check_rss_budget_devuelve_la_medicion_bajo_el_limite():
    current = check_rss_budget(1_000_000, context="test")
    assert current > 0.0


def test_check_rss_budget_aborta_limpio_sobre_el_limite():
    with pytest.raises(MemoryBudgetExceeded, match="presupuesto de memoria agotado"):
        check_rss_budget(1, context="tabla 1/2 dbo.T")


def test_limite_cero_desactiva_la_guarda():
    assert check_rss_budget(0, context="test") >= 0.0
