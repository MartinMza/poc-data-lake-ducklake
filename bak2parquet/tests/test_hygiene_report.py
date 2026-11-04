"""Tests sin motor: guarda del DROP, DROPCLEANBUFFERS y resumen (T7/S5)."""

import json

import pytest

from bak2parquet.hygiene import (
    RefusedDropError,
    drop_database,
    drop_database_sql,
    dropcleanbuffers_sql,
    is_owned_db_name,
    purge_buffers,
)
from bak2parquet.report import RunReport, TableResult, render_human


def test_dropcleanbuffers_sql():
    assert dropcleanbuffers_sql() == "DBCC DROPCLEANBUFFERS;"


class _Cur:
    def __init__(self, owner):
        self._owner = owner
        self._last = ""

    def execute(self, sql):
        self._owner.seen.append(sql)
        self._last = sql

    def fetchone(self):
        if "state_desc" in self._last:
            return None if self._owner.state is None else (self._owner.state,)
        raise AssertionError(f"fetchone inesperado tras: {self._last!r}")


class _Conn:
    def __init__(self, state="ONLINE"):
        self.seen = []
        self.state = state

    def cursor(self):
        return _Cur(self)


def test_purge_buffers_ejecuta_dbcc():
    conn = _Conn()
    purge_buffers(conn)
    assert conn.seen == ["DBCC DROPCLEANBUFFERS;"]


def test_drop_solo_nombre_generado_de_esta_corrida():
    owned = {"bak2pq_123_456"}
    assert is_owned_db_name("bak2pq_123_456", owned)
    # Mismo patrón pero de otra corrida: denegado.
    assert not is_owned_db_name("bak2pq_1_2", owned)
    # Nombres reales o derivados del .bak: siempre denegados.
    for otro in ("master", "RestoredProof", "FAF12_Kavos", "test-source", "", "bak2pq_no"):
        assert not is_owned_db_name(otro, owned | {otro})


def test_drop_ejecuta_y_se_niega():
    conn = _Conn(state="ONLINE")
    drop_database(conn, "bak2pq_123_456", owned={"bak2pq_123_456"})
    assert any("DROP DATABASE [bak2pq_123_456]" in s for s in conn.seen)
    assert any("SINGLE_USER" in s for s in conn.seen)
    conn2 = _Conn()
    with pytest.raises(RefusedDropError):
        drop_database(conn2, "FAF12_Kavos", owned={"bak2pq_123_456"})
    assert conn2.seen == []  # La guarda no tocó el motor.


def test_drop_en_restoring_no_usa_alter():
    """En RESTORING el ALTER falla con 5052: se hace DROP directo (higiene)."""
    conn = _Conn(state="RESTORING")
    drop_database(conn, "bak2pq_123_456", owned={"bak2pq_123_456"})
    assert any("DROP DATABASE [bak2pq_123_456]" in s for s in conn.seen)
    assert all("SINGLE_USER" not in s for s in conn.seen)


def test_drop_con_estado_ilegible_hace_drop_directo():
    """Si el estado no se puede leer, tampoco se deja basura: DROP directo."""
    conn = _Conn(state=None)
    drop_database(conn, "bak2pq_123_456", owned={"bak2pq_123_456"})
    assert any("DROP DATABASE [bak2pq_123_456]" in s for s in conn.seen)
    assert all("SINGLE_USER" not in s for s in conn.seen)


def test_drop_sql_con_escape():
    assert "[a]]b]" in drop_database_sql("a]b")


def _reporte():
    return RunReport(db_name="bak2pq_1_2", results=[
        TableResult("dbo", "A", rows=10, seconds=1.5),
        TableResult("dbo", "B", rows=0, seconds=0.2, ok=False, error="columna MAX"),
    ])


def test_reporte_conteos_exit_y_json(tmp_path):
    report = _reporte().finish()
    assert report.n_detected == 2
    assert report.n_ok == 1
    assert report.n_failed == 1
    assert report.total_rows == 10
    assert report.exit_code == 1  # Una tabla falló.
    ruta = report.write_json(str(tmp_path / "r.json"))
    datos = json.loads(open(ruta, encoding="utf-8").read())
    assert datos["detected"] == 2 and datos["failed"] == 1
    assert datos["tables"][1]["error"] == "columna MAX"
    ok = RunReport(db_name="x", results=[TableResult("dbo", "A", rows=1)]).finish()
    assert ok.exit_code == 0


def test_render_humano_con_tiempos_y_errores():
    texto = render_human(_reporte())
    assert "tablas detectadas: 2" in texto
    assert "filas procesadas: 10" in texto
    assert "dbo.A: 10 filas en 1.5s" in texto
    assert "ERROR dbo.B" in texto and "columna MAX" in texto


# --- Reaper de huérfanas (T3-T7): solo patrón propio, solo viejas. ---

from datetime import datetime, timedelta  # noqa: E402

from bak2parquet.hygiene import STALE_DATABASES_SQL, reap_stale_databases  # noqa: E402


class _ReapCursor:
    def __init__(self, owner):
        self._owner = owner
        self._last = ""

    def execute(self, sql):
        self._owner.seen.append(sql)
        self._last = sql

    def fetchall(self):
        if "create_date" in self._last:
            return list(self._owner.stale_rows)
        return []

    def fetchone(self):
        if "state_desc" in self._last:
            return (self._owner.state,)
        raise AssertionError(f"fetchone inesperado tras: {self._last!r}")


class _ReapConn:
    def __init__(self, stale_rows, state="ONLINE"):
        self.seen = []
        self.stale_rows = stale_rows
        self.state = state

    def cursor(self):
        return _ReapCursor(self)


def _old(minutes=120):
    return datetime.now() - timedelta(minutes=minutes)


def test_reaper_acepta_vieja_y_rechaza_fresca():
    """Vieja con patrón propio → DROP; fresca (corrida concurrente) → intacta."""
    conn = _ReapConn([
        ("bak2pq_11_22", _old(120)),  # huérfana: se reapea.
        ("bak2pq_33_44", datetime.now()),  # fresca: puede ser concurrente.
        ("FAF12_Kavos", _old(9999)),  # real aunque vieja: jamás se toca.
        ("bak2pqX_55_66", _old(9999)),  # casi-patrón (sin guion): no matchea.
        ("test-source", _old(9999)),  # otro nombre: no matchea.
    ])
    reaped = reap_stale_databases(conn, max_age_sec=3600)
    assert reaped == ["bak2pq_11_22"]
    drops = [s for s in conn.seen if "DROP DATABASE" in s]
    assert len(drops) == 1 and "[bak2pq_11_22]" in drops[0]
    assert all("FAF12_Kavos" not in s for s in conn.seen)


def test_reaper_cero_lo_desactiva():
    conn = _ReapConn([("bak2pq_11_22", _old(9999))])
    assert reap_stale_databases(conn, max_age_sec=0) == []
    assert conn.seen == []  # Ni siquiera consulta el catálogo.


def test_reaper_sin_patron_no_toca_nada():
    conn = _ReapConn([("master", _old(9999)), ("tempdb", _old(9999))])
    assert reap_stale_databases(conn, max_age_sec=60) == []
    assert not any("DROP DATABASE" in s for s in conn.seen)


def test_reaper_en_restoring_hace_drop_directo():
    """Huérfana en RESTORING: el ALTER fallaría con 5052, DROP directo."""
    conn = _ReapConn([("bak2pq_77_88", _old(9999))], state="RESTORING")
    assert reap_stale_databases(conn, max_age_sec=60) == ["bak2pq_77_88"]
    drops = [s for s in conn.seen if "DROP DATABASE" in s]
    assert len(drops) == 1 and "SINGLE_USER" not in drops[0]


def test_stale_sql_escapa_el_guion_bajo():
    """`LIKE 'bak2pq_%'` sin escape matchearía `bak2pqX...`: el escape es guarda."""
    assert "bak2pq\\_%" in STALE_DATABASES_SQL
    assert "ESCAPE" in STALE_DATABASES_SQL
