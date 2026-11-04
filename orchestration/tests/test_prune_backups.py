"""T7 / Spec S8: poda de backups reteniendo los N más nuevos por NOMBRE.

El disco crece ~60 GB/mes y cada backup pesa ~7,5 GB, así que la poda tiene
que borrar el backup correcto. El riesgo que fijan estos tests: ordenar por
mtime en vez de por nombre borraría el backup equivocado después de un `cp`
o una re-descarga. Por eso el test clave invierte los mtimes a propósito.

Se corre el script como subproceso (como un operador lo invocaría) y se
afirman efectos observables: archivos que quedan en disco, exit code y el
resumen impreso. Los archivos son chicos pero con nombres reales de backup.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prune_backups.py"

# Tres backups con fechas crecientes en el nombre (formato real de Finnegans).
VIEJO = "FAF12_Kavos_2026_10_07_233301_411720.bak"
MEDIO = "FAF12_Kavos_2026_10_25_120000_000001.bak.tar.gz"
NUEVO = "FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz"


def _sembrar(destino: Path, *nombres: str) -> None:
    """Crea archivos chicos con nombres reales de backup."""
    for nombre in nombres:
        (destino / nombre).write_bytes(b"backup-falso")


def _correr(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Corre el script aislado de `$PRUNE_DIR` salvo que el test lo pida."""
    env = dict(os.environ)
    env.pop("PRUNE_DIR", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _nombres(destino: Path) -> set[str]:
    return {p.name for p in destino.iterdir() if p.is_file()}


def test_conserva_los_n_mas_nuevos_por_nombre_aunque_mtime_diga_lo_contrario(tmp_path):
    # El backup más viejo se toca al final: ordenar por mtime conservaría el
    # archivo equivocado. El script debe ignorar el mtime por completo.
    _sembrar(tmp_path, VIEJO, MEDIO, NUEVO)
    ahora = time.time()
    os.utime(tmp_path / NUEVO, (ahora - 300, ahora - 300))
    os.utime(tmp_path / VIEJO, (ahora, ahora))

    proc = _correr(str(tmp_path), "--keep", "2", "--apply")

    assert proc.returncode == 0
    assert _nombres(tmp_path) == {MEDIO, NUEVO}


def test_dry_run_por_defecto_no_borra_nada(tmp_path):
    _sembrar(tmp_path, VIEJO, MEDIO, NUEVO)

    proc = _correr(str(tmp_path), "--keep", "1")

    assert proc.returncode == 0
    assert _nombres(tmp_path) == {VIEJO, MEDIO, NUEVO}
    assert "Dry-run" in proc.stdout
    assert NUEVO in proc.stdout  # el plan nombra lo que conservaría


def test_apply_borra_exactamente_el_excedente(tmp_path):
    _sembrar(tmp_path, VIEJO, MEDIO, NUEVO)

    proc = _correr(str(tmp_path), "--keep", "2", "--apply")

    assert proc.returncode == 0
    assert _nombres(tmp_path) == {MEDIO, NUEVO}
    assert "borrados=1" in proc.stdout
    assert "liberados" in proc.stdout


def test_incoming_con_part_en_vuelo_no_se_toca(tmp_path):
    # Un backup viejísimo dentro de .incoming tentaría a la poda si descendiera.
    incoming = tmp_path / ".incoming"
    incoming.mkdir()
    (incoming / "x.bak.part").write_bytes(b"descarga-en-curso")
    (incoming / "FAF12_Kavos_2020_01_01_000000_000000.bak.tar.gz").write_bytes(b"viejisimo")
    _sembrar(tmp_path, VIEJO, MEDIO, NUEVO)

    proc = _correr(str(tmp_path), "--keep", "1", "--apply")

    assert proc.returncode == 0
    assert _nombres(tmp_path) == {NUEVO}
    assert (incoming / "x.bak.part").read_bytes() == b"descarga-en-curso"
    assert (incoming / "FAF12_Kavos_2020_01_01_000000_000000.bak.tar.gz").exists()


def test_lo_no_parseable_nunca_se_borra_y_se_reporta(tmp_path):
    _sembrar(tmp_path, MEDIO, NUEVO, "notas.txt", "FAF12_Kavos_final.bak")

    proc = _correr(str(tmp_path), "--keep", "1", "--apply")

    assert proc.returncode == 0
    assert _nombres(tmp_path) == {NUEVO, "notas.txt", "FAF12_Kavos_final.bak"}
    assert "ignorado" in proc.stdout.lower()
    assert "notas.txt" in proc.stdout
    assert "FAF12_Kavos_final.bak" in proc.stdout


def test_keep_1_deja_exactamente_el_mas_nuevo(tmp_path):
    _sembrar(tmp_path, VIEJO, MEDIO, NUEVO)

    proc = _correr(str(tmp_path), "--keep", "1", "--apply")

    assert proc.returncode == 0
    assert _nombres(tmp_path) == {NUEVO}


def test_keep_0_falla_sin_borrar_nada(tmp_path):
    _sembrar(tmp_path, VIEJO, MEDIO, NUEVO)

    proc = _correr(str(tmp_path), "--keep", "0", "--apply")

    assert proc.returncode != 0
    assert _nombres(tmp_path) == {VIEJO, MEDIO, NUEVO}


def test_prune_dir_como_variable_de_entorno(tmp_path):
    # Sin argv posicional, el destino sale de $PRUNE_DIR.
    _sembrar(tmp_path, VIEJO, MEDIO, NUEVO)

    proc = _correr("--keep", "1", "--apply", env_extra={"PRUNE_DIR": str(tmp_path)})

    assert proc.returncode == 0
    assert _nombres(tmp_path) == {NUEVO}
