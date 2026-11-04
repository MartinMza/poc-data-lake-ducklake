"""T7 / Spec S8: retención de exactamente un backup.

Cambio de requisito (L10): "siempre sobre escribir y dejar un solo archivo
backup". La lógica vive acá y la usan **los dos** consumidores —el flow, que
poda automáticamente después de adquirir, y `scripts/prune_backups.py`, que es
el CLI para correrlo a mano—. Dos implementaciones de borrado distintas es
exactamente cómo se pierde un backup.

Lo que estos tests fijan:

- El orden es por el timestamp del **nombre**, nunca por mtime. Un `cp` o una
  re-descarga cambia el mtime y ordenar por él borraría el backup bueno.
- Lo que no parsea como backup fechado no se borra jamás: los fixtures del
  repo (`test-source.bak`, `bak2pq-synth.bak`) no son artefactos de producción.
- Nunca se borra el más nuevo, ni con `keep` mal configurado.
- No se desciende a subdirectorios: `.incoming/` tiene `.part` en vuelo.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from kavos_acquire.retention import apply_retention, plan_retention

VIEJO = "FAF12_Kavos_2026_10_07_233301_411720.bak"
MEDIO = "FAF12_Kavos_2026_10_25_120000_000001.bak.tar.gz"
NUEVO = "FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz"


def _sembrar(destino: Path, *nombres: str, tamano: int = 10) -> None:
    for nombre in nombres:
        (destino / nombre).write_bytes(b"x" * tamano)


def _nombres(rutas) -> set[str]:
    return {path.name for _, path in rutas}


def test_con_un_solo_backup_no_hay_nada_que_borrar(tmp_path):
    _sembrar(tmp_path, NUEVO)

    plan = plan_retention(tmp_path, keep=1)

    assert _nombres(plan.keep) == {NUEVO}
    assert plan.delete == ()


def test_keep_uno_deja_solo_el_mas_nuevo(tmp_path):
    _sembrar(tmp_path, VIEJO, MEDIO, NUEVO)

    plan = plan_retention(tmp_path, keep=1)

    assert _nombres(plan.keep) == {NUEVO}
    assert _nombres(plan.delete) == {VIEJO, MEDIO}


def test_el_orden_es_por_nombre_y_no_por_mtime(tmp_path):
    # El más viejo se toca al final: ordenar por mtime conservaría el equivocado
    # y borraría el backup del día.
    _sembrar(tmp_path, VIEJO, MEDIO, NUEVO)
    ahora = time.time()
    os.utime(tmp_path / NUEVO, (ahora - 300, ahora - 300))
    os.utime(tmp_path / VIEJO, (ahora, ahora))

    plan = plan_retention(tmp_path, keep=1)

    assert _nombres(plan.keep) == {NUEVO}
    assert _nombres(plan.delete) == {VIEJO, MEDIO}


def test_lo_que_no_parsea_como_backup_nunca_se_borra(tmp_path):
    _sembrar(tmp_path, VIEJO, NUEVO, "test-source.bak", "bak2pq-synth.bak", "notas.txt")

    plan = plan_retention(tmp_path, keep=1)

    assert _nombres(plan.keep) == {NUEVO}
    assert _nombres(plan.delete) == {VIEJO}
    ignorados = {path.name for path, _ in plan.ignored}
    assert ignorados == {"test-source.bak", "bak2pq-synth.bak", "notas.txt"}


def test_no_desciende_a_subdirectorios(tmp_path):
    entrada = tmp_path / ".incoming"
    entrada.mkdir()
    (entrada / "FAF12_Kavos_2026_01_01_000000_000000.bak.part").write_bytes(b"en vuelo")
    _sembrar(tmp_path, VIEJO, NUEVO)

    plan = plan_retention(tmp_path, keep=1)
    apply_retention(plan)

    assert (entrada / "FAF12_Kavos_2026_01_01_000000_000000.bak.part").exists()


def test_keep_cero_se_coacciona_y_nunca_vacia_el_directorio(tmp_path):
    # Última defensa: un `keep` mal configurado no puede dejar el lago sin
    # backup del cual recuperarse.
    _sembrar(tmp_path, VIEJO, NUEVO)

    plan = plan_retention(tmp_path, keep=0)
    apply_retention(plan)

    assert _nombres(plan.delete) == {VIEJO}
    assert {p.name for p in tmp_path.iterdir()} == {NUEVO}


def test_apply_borra_exactamente_el_plan_y_devuelve_los_paths(tmp_path):
    _sembrar(tmp_path, VIEJO, MEDIO, NUEVO, tamano=32)

    plan = plan_retention(tmp_path, keep=1)
    borrados = apply_retention(plan)

    assert {p.name for p in borrados} == {VIEJO, MEDIO}
    assert {p.name for p in tmp_path.iterdir()} == {NUEVO}
    assert plan.freed_bytes == 64


def test_directorio_inexistente_es_plan_vacio(tmp_path):
    plan = plan_retention(tmp_path / "no-existe", keep=1)

    assert plan.keep == ()
    assert plan.delete == ()
    assert apply_retention(plan) == []


def test_borra_el_bak_derivado_y_deja_el_comprimido(tmp_path):
    """Requisito L10: un solo archivo. El .bak extraído es derivado."""
    from kavos_acquire.retention import remove_extracted_bak

    archivo = tmp_path / "FAF12_Kavos_2026_11_03_233302_428734.bak.tar.gz"
    derivado = tmp_path / "FAF12_Kavos_2026_11_03_233302_428734.bak"
    archivo.write_bytes(b"comprimido")
    derivado.write_bytes(b"extraido")

    assert remove_extracted_bak(archivo, derivado) is True
    assert {p.name for p in tmp_path.iterdir()} == {archivo.name}


def test_no_borra_el_derivado_si_el_comprimido_ya_no_esta(tmp_path):
    # Sin el comprimido, el .bak es la única copia: no se toca.
    from kavos_acquire.retention import remove_extracted_bak

    derivado = tmp_path / "FAF12_Kavos_2026_11_03_233302_428734.bak"
    derivado.write_bytes(b"unico")
    faltante = tmp_path / "FAF12_Kavos_2026_11_03_233302_428734.bak.tar.gz"

    assert remove_extracted_bak(faltante, derivado) is False
    assert derivado.exists()


def test_no_borra_si_las_rutas_son_la_misma(tmp_path):
    from kavos_acquire.retention import remove_extracted_bak

    unico = tmp_path / "FAF12_Kavos_2026_11_03_233302_428734.bak"
    unico.write_bytes(b"bak suelto que dejo el operador")

    assert remove_extracted_bak(unico, unico) is False
    assert unico.exists()
