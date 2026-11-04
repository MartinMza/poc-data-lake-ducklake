"""La guarda del DROP: lo único destructivo del sistema.

Estos tests no verifican que el DROP funcione, verifican que **no se emita
ninguna sentencia destructiva** cuando el nombre no pertenece al conjunto
creado por la corrida. Una guarda que se equivoca una vez borra una base real.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from bak2parquet.hygiene import (
    RefusedDropError,
    drop_database,
    is_owned_db_name,
    reap_stale_databases,
)


class _Cursor:
    def __init__(self, owner: "_Conn") -> None:
        self._owner = owner
        self._last = ""

    def execute(self, sql: str) -> None:
        self._owner.sql.append(sql)
        self._last = sql

    def fetchall(self):
        return list(self._owner.rows)

    def fetchone(self):
        if "state_desc" in self._last:
            return (self._owner.state,)
        return None

    def nextset(self) -> bool:
        return False


class _Conn:
    """Conexión falsa que registra cada sentencia emitida."""

    def __init__(self, rows=(), state="ONLINE") -> None:
        self.sql: list[str] = []
        self.rows = list(rows)
        self.state = state

    def destructive(self) -> list[str]:
        return [s for s in self.sql if "DROP DATABASE" in s]

    def cursor(self):
        return _Cursor(self)


OWNED = {"bak2pq_1_2"}


@pytest.mark.parametrize(
    "name",
    ["master", "model", "bak2pq_999_999", "mi_backup.bak", "", "bak2pq_1_2; DROP DATABASE x"],
)
def test_un_nombre_ajeno_se_rechaza_sin_emitir_sql(name):
    conn = _Conn()
    with pytest.raises(RefusedDropError):
        drop_database(conn, name, owned=OWNED)
    assert conn.sql == [], "la guarda debe negarse ANTES de tocar el motor"


def test_el_patron_no_alcanza_por_si_solo():
    """Estar en el conjunto no basta: el nombre también debe cumplir el patrón."""
    assert is_owned_db_name("bak2pq_1_2", OWNED)
    assert not is_owned_db_name("bak2pq_abc", {"bak2pq_abc"})
    assert not is_owned_db_name("bak2pq_1_2", {"otra"})


def test_un_nombre_propio_si_se_borra():
    conn = _Conn(state="ONLINE")
    drop_database(conn, "bak2pq_1_2", owned=OWNED)
    assert len(conn.destructive()) == 1


def test_una_base_en_restoring_se_borra_directo_sin_alter():
    """El ALTER falla con 5052 sobre RESTORING y dejaría la base huérfana."""
    conn = _Conn(state="RESTORING")
    drop_database(conn, "bak2pq_1_2", owned=OWNED)
    assert len(conn.destructive()) == 1
    assert "ALTER DATABASE" not in conn.destructive()[0]


def test_el_reaper_respeta_la_edad_y_no_toca_lo_reciente():
    """Una corrida concurrente no puede perder su base por el reaper."""
    ahora = datetime.now()
    vieja = ahora - timedelta(hours=2)
    fresca = ahora - timedelta(seconds=10)

    conn = _Conn(rows=[("bak2pq_1_1", vieja)])
    assert reap_stale_databases(conn, max_age_sec=3600, now=ahora) == ["bak2pq_1_1"]
    assert len(conn.destructive()) == 1

    conn = _Conn(rows=[("bak2pq_1_3", fresca)])
    assert reap_stale_databases(conn, max_age_sec=3600, now=ahora) == []
    assert conn.destructive() == []


def test_el_reaper_no_borra_nombres_que_no_genero():
    ahora = datetime.now()
    vieja = ahora - timedelta(hours=5)
    conn = _Conn(rows=[("FAF12_Kavos", vieja), ("bak2pqX_1_2", vieja)])
    assert reap_stale_databases(conn, max_age_sec=60, now=ahora) == []
    assert conn.destructive() == []


def test_el_reaper_se_desactiva_con_edad_cero():
    conn = _Conn(rows=[("bak2pq_1_1", datetime.now() - timedelta(days=5))])
    assert reap_stale_databases(conn, max_age_sec=0) == []
    assert conn.sql == []
