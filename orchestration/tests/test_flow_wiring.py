"""T6 / Specs S7 y S9: cableado del flow (puro, sin Prefect ni red).

Fija el contrato que une `acquire` con la ingesta sin correr el flow:

- `bak_path_for`: lo adquirido manda; sin adquisición vale `MSSQL_BAK_PATH`.
- `cron_from_hours`: el schedule declarado de `serve.py`.
- `should_mark_applied`: solo se marca aplicado con carga exitosa y backup.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flow import bak_path_for, should_mark_applied
from kavos_acquire.identity import parse_backup_id
from kavos_acquire.pipeline import (
    STATUS_ACQUIRED,
    STATUS_NOTHING,
    AcquireOutcome,
)
from serve import cron_from_hours

NUEVO = "FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz"


def _adquirido() -> AcquireOutcome:
    """Un outcome recién bajado a `mssql-backup/`, como lo devuelve `acquire`."""
    return AcquireOutcome(
        status=STATUS_ACQUIRED,
        path=Path("/repo/mssql-backup") / NUEVO,
        backup_id=parse_backup_id(NUEVO),
        message=f"adquirido {NUEVO}",
    )


def _sin_nada() -> AcquireOutcome:
    """Un outcome sin backup nuevo: el flow debe seguir el camino manual."""
    return AcquireOutcome(status=STATUS_NOTHING, message="sin correos nuevos")


# --- bak_path_for: lo adquirido manda ---------------------------------------


def test_lo_adquirido_manda_sin_reapuntar_el_env():
    env = "/otro/lado/FAF12_Kavos_viejo.bak"

    assert bak_path_for(_adquirido(), env) == f"/repo/mssql-backup/{NUEVO}"


def test_sin_backup_nuevo_vale_el_env():
    env = "mssql-backup/FAF12_Kavos_2026_10_07_233301_411720.bak"

    assert bak_path_for(_sin_nada(), env) == env


def test_sin_outcome_vale_el_env():
    env = "mssql-backup/manual.bak"

    assert bak_path_for(None, env) == env


def test_el_env_vacio_se_respeta_cuando_no_hay_adquisicion():
    # Sin adquisición y sin MSSQL_BAK_PATH, `bak-extract` se salta con
    # warning: el workflow manual queda intacto.
    assert bak_path_for(_sin_nada(), "") == ""


def test_un_adquirido_sin_path_no_rompe_el_env():
    roto = AcquireOutcome(status=STATUS_ACQUIRED, path=None)

    assert bak_path_for(roto, "mssql-backup/manual.bak") == "mssql-backup/manual.bak"


# --- cron_from_hours: schedule declarado -------------------------------------


def test_cron_cada_hora():
    assert cron_from_hours(1) == "0 * * * *"


def test_cron_cada_cuatro_horas_es_el_default():
    assert cron_from_hours(4) == "0 */4 * * *"


def test_cron_diario_a_medianoche():
    assert cron_from_hours(24) == "0 0 * * *"


@pytest.mark.parametrize("horas", [0, -1, 25, 48])
def test_cron_fuera_de_rango_falla(horas: int):
    with pytest.raises(ValueError):
        cron_from_hours(horas)


# --- should_mark_applied: nunca marcar si la carga falló ---------------------


def test_con_carga_ok_y_backup_se_marca():
    assert should_mark_applied(_adquirido(), True) is True


def test_con_carga_fallida_no_se_marca_aunque_haya_backup():
    assert should_mark_applied(_adquirido(), False) is False


def test_con_carga_ok_pero_sin_backup_no_se_marca():
    assert should_mark_applied(_sin_nada(), True) is False


def test_sin_outcome_no_se_marca():
    assert should_mark_applied(None, True) is False
    assert should_mark_applied(None, False) is False
