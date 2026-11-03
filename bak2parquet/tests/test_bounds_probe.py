"""Regresiones de los techos de buffer (sonda, inventario y reintento).

Las tres nacieron de fallos reales sobre el backup de 7 GB: 13 tablas quedaban
en error aunque entraban holgadas en el presupuesto.
"""

from __future__ import annotations

from bak2parquet import catalog, cli
from bak2parquet.catalog import TableRef
from bak2parquet.config import load_config

MENSAJE_TRUNCAMIENTO = (
    "External error: A value (at least one) is too large to be written into the "
    "allocated buffer without truncation. Size in bytes indicated by ODBC driver"
)
MENSAJE_FILA_GRANDE = (
    "The Odbc buffer is limited to a size of 2097152 bytes. Yet a single row does "
    "require up to 4200621."
)


def test_el_inventario_de_columnas_max_incluye_lobs_legacy():
    """`text`/`ntext`/`image` no tienen max_length = -1 en sys.columns (valen 16)."""
    sql = catalog.MAX_COLUMNS_SQL.lower()
    for legacy in ("text", "ntext", "image"):
        assert legacy in sql, f"{legacy} quedaría fuera de la sonda"
    assert "max_length = -1" in sql


def test_la_sonda_por_tabla_incluye_lobs_legacy():
    sql = catalog.max_column_names_sql("db").lower()
    for legacy in ("text", "ntext", "image"):
        assert legacy in sql


def test_el_reintento_reconoce_los_dos_mensajes_de_arrow_odbc():
    """El mensaje de truncamiento era el que aparecía en el backup real."""
    assert cli._buffer_too_small(Exception(MENSAJE_TRUNCAMIENTO))
    assert cli._buffer_too_small(Exception(MENSAJE_FILA_GRANDE))


def test_un_error_ajeno_no_dispara_el_reintento():
    assert not cli._buffer_too_small(Exception("Cannot open database requested by the login"))


def test_tabla_sin_columnas_max_no_falla_ni_ajusta(tmp_path, monkeypatch):
    """Antes lanzaba _TableTooBig con un mensaje absurdo (241 MB < límite 400)."""
    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    cfg = load_config({"bak": str(bak), "out": "o"}, env={"MSSQL_SA_PASSWORD": "x"})
    monkeypatch.setattr(cli, "max_column_names", lambda conn, ref, db: [])
    resultado = cli._tighten_bounds(
        conn=None, cfg=cfg, db_name="x", ref=TableRef("dbo", "T"), n_text=0, n_binary=0
    )
    assert resultado is cfg
