"""T1 / Spec S3: identidad del backup y guarda de monotonicidad.

El nombre del artefacto codifica la fecha del backup. `bronze-load` reemplaza
tablas enteras, así que cargar un backup más viejo retrocede el lago en el
tiempo sin ningún error. Estos tests fijan esa guarda.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from kavos_acquire.identity import (
    BackupId,
    BackupNameError,
    StaleBackupError,
    guard_monotonic,
    parse_backup_id,
)

# Nombres reales de Finnegans: el del 2026-11-01 es el que llegó por S3 y el del
# 2026-10-07 es el `.bak` que hoy está en mssql-backup/.
S3_ARTIFACT = "FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz"
PREVIOUS_BAK = "FAF12_Kavos_2026_10_07_233301_411720.bak"


def test_parsea_el_artefacto_real_de_s3():
    backup = parse_backup_id(S3_ARTIFACT)

    assert backup.stamp == datetime(2026, 11, 1, 23, 33, 2)
    assert backup.fraction == 905705
    assert backup.filename == S3_ARTIFACT


def test_parsea_el_bak_plano_previo():
    backup = parse_backup_id(PREVIOUS_BAK)

    assert backup.stamp == datetime(2026, 10, 7, 23, 33, 1)
    assert backup.fraction == 411720


def test_ordena_por_la_fecha_del_nombre():
    assert parse_backup_id(S3_ARTIFACT) > parse_backup_id(PREVIOUS_BAK)


def test_la_fraccion_desempata_dentro_del_mismo_segundo():
    antes = parse_backup_id("FAF12_Kavos_2026_11_01_233302_123456.bak.tar.gz")
    despues = parse_backup_id("FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz")

    assert antes.stamp == despues.stamp
    assert despues > antes


def test_un_nombre_sin_timestamp_es_invalido():
    with pytest.raises(BackupNameError) as exc:
        parse_backup_id("FAF12_Kavos_ultimo.bak")

    assert "FAF12_Kavos_ultimo.bak" in str(exc.value)


def test_una_fecha_imposible_es_invalida():
    # Mes 13: el regex matchea, el calendario no.
    with pytest.raises(BackupNameError):
        parse_backup_id("FAF12_Kavos_2026_13_01_233302_905705.bak.tar.gz")


def test_una_extension_que_no_es_backup_es_invalida():
    with pytest.raises(BackupNameError):
        parse_backup_id("FAF12_Kavos_2026_11_01_233302_905705.csv")


def test_la_guarda_rechaza_un_backup_mas_viejo():
    ultimo = parse_backup_id(S3_ARTIFACT)

    with pytest.raises(StaleBackupError) as exc:
        guard_monotonic(parse_backup_id(PREVIOUS_BAK), ultimo)

    mensaje = str(exc.value)
    assert "2026-10-07" in mensaje
    assert "2026-11-01" in mensaje


def test_la_guarda_rechaza_el_mismo_backup_ya_aplicado():
    # Un reintento sobre el mismo artefacto no debe re-disparar la ingesta.
    ultimo = parse_backup_id(S3_ARTIFACT)

    with pytest.raises(StaleBackupError):
        guard_monotonic(parse_backup_id(S3_ARTIFACT), ultimo)


def test_la_guarda_acepta_lo_nuevo_y_el_primer_backup():
    assert guard_monotonic(parse_backup_id(S3_ARTIFACT), None) is None
    assert (
        guard_monotonic(
            parse_backup_id(S3_ARTIFACT), parse_backup_id(PREVIOUS_BAK)
        )
        is None
    )


def test_el_estado_serializa_y_vuelve_sin_perder_el_orden():
    original = parse_backup_id(S3_ARTIFACT)

    reconstruido = BackupId.from_dict(original.to_dict())

    assert reconstruido == original
