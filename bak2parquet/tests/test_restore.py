"""Tests sin motor: SQL generado de FILELISTONLY/MOVE y aborto (T4/S2)."""

import pytest

from bak2parquet.restore import (
    FileEntry,
    RestoreError,
    build_move_list,
    filelistonly_sql,
    parse_filelist_rows,
    restore_database,
    restore_sql,
    sanitize_file_stem,
)


def test_filelistonly_con_ruta_escapada():
    sql = filelistonly_sql("/var/opt/mssql/backup/x'.bak")
    assert "RESTORE FILELISTONLY" in sql
    assert "x''.bak" in sql  # Comilla duplicada.


def test_parse_acepta_dicts_y_tuplas_e_ignora_otros_tipos():
    rows = [
        {"LogicalName": "MiBase", "Type": "D"},
        {"LogicalName": "MiBase_log", "Type": "L"},
        {"LogicalName": "Filestream", "Type": "S"},  # Se ignora (case *).
        ("Otra", "/fisico.mdf", "d"),  # Tupla, minúscula.
        ("corta",),  # Ilegible: se ignora.
    ]
    entries = parse_filelist_rows(rows)
    assert entries == [FileEntry("MiBase", "D"), FileEntry("MiBase_log", "L"), FileEntry("Otra", "D")]


def test_move_mdf_ndf_ldf_y_saneado():
    entries = [FileEntry("Base", "D"), FileEntry("Base2", "D"), FileEntry("Base_log", "L")]
    moves = build_move_list(entries, db_name="bak2pq_1_2",
                            data_dir="/var/opt/mssql/data", log_dir="/var/opt/mssql/log/")
    assert moves[0].endswith("bak2pq_1_2_Base.mdf'")
    assert ".ndf'" in moves[1]
    assert moves[2].endswith("bak2pq_1_2_Base_log.ldf'")
    assert all(m.startswith("MOVE N'") for m in moves)


def test_sanitize_file_stem_como_tr():
    assert sanitize_file_stem("Mi Base/Datos:lóg") == "Mi_Base_Datos_l_g"


def test_restore_sql_con_replace_recovery():
    sql = restore_sql("bak2pq_1_2", "/b/x.bak", ["MOVE N'a' TO N'b'"])
    assert "RESTORE DATABASE [bak2pq_1_2]" in sql
    assert "REPLACE" in sql and "RECOVERY" in sql
    assert "MOVE N'a' TO N'b'" in sql


class _FakeCursor:
    def __init__(self, owner):
        self._owner = owner
        self._last_sql = ""

    def execute(self, sql):
        self._owner.seen.append(sql)
        self._last_sql = sql
        if "RESTORE DATABASE" in sql and self._owner.fail_restore:
            raise RuntimeError("Operating system error 112 (no hay espacio).")
        return None

    def fetchall(self):
        return [{"LogicalName": "B", "Type": "D"}, {"LogicalName": "B_log", "Type": "L"}]

    def fetchone(self):
        if "state_desc" in self._last_sql:
            return (self._owner.state,)
        return ("/var/opt/mssql/data/", "/var/opt/mssql/data/")

    def nextset(self):
        self._owner.nextset_calls += 1
        if self._owner.extra_sets > 0:
            self._owner.extra_sets -= 1
            return True
        return False


class _FakeConn:
    def __init__(self, fail_restore=False, state="ONLINE", extra_sets=2):
        self.seen = []
        self.fail_restore = fail_restore
        self.state = state
        self.extra_sets = extra_sets
        self.nextset_calls = 0

    def cursor(self):
        return _FakeCursor(self)


def test_restore_orquesta_filelist_move_restore():
    conn = _FakeConn()
    moves = restore_database(conn, db_name="bak2pq_9_9", container_bak="/b/f.bak")
    assert len(moves) == 2
    assert any("FILELISTONLY" in s for s in conn.seen)
    assert any("RESTORE DATABASE [bak2pq_9_9]" in s for s in conn.seen)


def test_restore_aborta_con_motivo_del_motor():
    conn = _FakeConn(fail_restore=True)
    with pytest.raises(RestoreError, match="no hay espacio"):
        restore_database(conn, db_name="bak2pq_9_9", container_bak="/b/f.bak")


def test_restore_drena_los_resultados_antes_de_volver():
    """El RESTORE emite su fase de recovery como juegos extra: hay que drenarlos.

    Si el código vuelve tras `execute()` sin consumir `nextset()` hasta el
    final, el motor abandona el recovery y la base queda en RESTORING para
    siempre (visto en vivo: sqlcmd drena y llega a ONLINE, pyodbc sin drenar
    no). Este test falla si se quita el drenado."""
    conn = _FakeConn(extra_sets=2)
    restore_database(conn, db_name="bak2pq_9_9", container_bak="/b/f.bak")
    # 2 juegos extra + el intento final que ya no trae nada.
    assert conn.nextset_calls == 3


def test_restore_verifica_que_la_base_llego_a_online():
    """Tras drenar, el restore exige ONLINE y aborta si no llega (S2.3)."""
    conn = _FakeConn()
    restore_database(conn, db_name="bak2pq_9_9", container_bak="/b/f.bak")
    assert any("state_desc" in s for s in conn.seen)


def test_restore_falla_si_la_base_no_llega_a_online():
    """Una base que se queda en RESTORING aborta con el motivo, no sigue."""
    conn = _FakeConn(state="RESTORING")
    with pytest.raises(RestoreError, match="ONLINE"):
        restore_database(conn, db_name="bak2pq_9_9", container_bak="/b/f.bak",
                         wait_online_sec=0)


def test_restore_aborta_sin_datos():
    class _Vacia(_FakeCursor):
        def fetchall(self):
            return [{"LogicalName": "solo_log", "Type": "L"}]

    class _ConnVacia(_FakeConn):
        def cursor(self):
            return _Vacia(self)

    with pytest.raises(RestoreError, match="no declara archivos de datos"):
        restore_database(_ConnVacia(), db_name="bak2pq_9_9", container_bak="/b/f.bak")
