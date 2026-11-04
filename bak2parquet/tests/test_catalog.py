"""Tests sin motor: catálogo de tablas base por schema (T5/S3.1-S3.2)."""

from bak2parquet.catalog import CATALOG_SQL, TableRef, list_tables, rows_to_tables, table_query


def test_catalog_query_solo_tablas_base_de_usuario():
    assert "sys.tables" in CATALOG_SQL
    assert "'U'" in CATALOG_SQL  # Tablas de usuario (no vistas).
    assert "is_ms_shipped = 0" in CATALOG_SQL  # Sin tablas de sistema.
    assert "schema_name" in CATALOG_SQL and "table_name" in CATALOG_SQL


def test_rows_to_tables_preserva_schema():
    rows = [{"schema_name": "dbo", "table_name": "BSMoneda"},
            ("ventas", "DMart_Ventas"),
            ("solo-uno",)]
    tables = rows_to_tables(rows)
    assert tables == [TableRef("dbo", "BSMoneda"), TableRef("ventas", "DMart_Ventas")]
    assert tables[0].display == "dbo.BSMoneda"


def test_list_tables_usa_conexion_de_control():
    class _Cur:
        def execute(self, sql):
            assert sql is CATALOG_SQL

        def fetchall(self):
            return [{"schema_name": "dbo", "table_name": "T"}]

    class _Conn:
        def cursor(self):
            return _Cur()

    assert list_tables(_Conn()) == [TableRef("dbo", "T")]


def test_table_query_con_nolock_y_escape():
    sql = table_query(TableRef("dbo", "BSMoneda"))
    assert sql == "SELECT * FROM [dbo].[BSMoneda] WITH (NOLOCK);"
    assert table_query(TableRef("a]b", "c")).startswith("SELECT * FROM [a]]b].[c]")


# --- Inventario de columnas MAX (T3-T7): el modelo de memoria escala. ---

from bak2parquet.catalog import (  # noqa: E402
    count_all_max_columns,
    max_column_names,
    max_columns_for,
    probe_max_lengths_sql,
)


class _CatCursor:
    def __init__(self, owner):
        self._owner = owner
        self._last = ""

    def execute(self, sql):
        self._owner.seen.append(sql)
        self._last = sql

    def fetchall(self):
        if "COUNT" in self._last:
            return list(self._owner.count_rows)
        if "[sys].[columns]" in self._last or "sys.columns" in self._last:
            return list(self._owner.name_rows)
        return []

    def fetchone(self):
        if "DATALENGTH" in self._last:
            return tuple(self._owner.probe_row)
        raise AssertionError(f"fetchone inesperado tras: {self._last!r}")


class _CatConn:
    def __init__(self, count_rows=(), name_rows=(), probe_row=()):
        self.seen = []
        self.count_rows = count_rows
        self.name_rows = name_rows
        self.probe_row = probe_row

    def cursor(self):
        return _CatCursor(self)


def test_count_all_max_columns_agrupa_texto_y_binario():
    from bak2parquet.catalog import TableRef  # noqa

    conn = _CatConn(count_rows=[
        ("dbo", "Ancha", "varchar", 30),
        ("dbo", "Ancha", "nvarchar", 2),
        ("dbo", "Ancha", "varbinary", 1),
        ("dbo", "Angosta", "varchar", 1),
    ])
    counts = count_all_max_columns(conn)
    assert counts == {("dbo", "Ancha"): (32, 1), ("dbo", "Angosta"): (1, 0)}
    assert max_columns_for(TableRef("dbo", "Ancha"), counts) == (32, 1)
    assert max_columns_for(TableRef("dbo", "SinMax"), counts) == (0, 0)


def test_max_column_names_clasifica_texto_binario():
    from bak2parquet.catalog import TableRef  # noqa

    conn = _CatConn(name_rows=[("nota", "varchar"), ("doc", "varbinary"), ("ficha", "xml")])
    assert max_column_names(conn, TableRef("dbo", "T"), "db") == [
        ("nota", "text"), ("doc", "binary"), ("ficha", "text")]
    sql = conn.seen[0]
    # La base va calificada: la conexión de control vive en master y sin
    # calificar la tabla no aparece (era la causa de los 4 truncamientos).
    assert "[db].[sys].[columns]" in sql and "max_length = -1" in sql


def test_probe_sql_un_solo_scan_con_datalength():
    from bak2parquet.catalog import TableRef  # noqa

    sql = probe_max_lengths_sql("bak2pq_1_2", TableRef("dbo", "T"),
                                [("nota", "text"), ("doc", "binary")])
    assert sql.count("DATALENGTH") == 2
    assert "[bak2pq_1_2].[dbo].[T]" in sql and "NOLOCK" in sql
