"""Tests sin motor: extractor con batches sintéticos (T6/S3.3/S4).

La costura `BatchSource` permite afirmar conteos, compresión ZSTD, tipos
preservados y tabla vacía con schema, sin SQL Server.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from bak2parquet.catalog import TableRef
from bak2parquet.extract import BatchSource, dest_for, extract_table


def _schema():
    return pa.schema([
        ("id", pa.int64()),
        ("precio", pa.decimal128(10, 2)),
        ("ts", pa.timestamp("us")),
        ("activo", pa.bool_()),
        ("nombre", pa.string()),
        ("nota", pa.string()),
    ])


def _batch(start, n):
    return pa.record_batch(
        [
            pa.array([start + i for i in range(n)], type=pa.int64()),
            pa.array([Decimal(f"{start + i}.99") for i in range(n)], type=pa.decimal128(10, 2)),
            pa.array([datetime(2024, 1, 1 + i, tzinfo=timezone.utc) for i in range(n)],
                     type=pa.timestamp("us")),
            pa.array([i % 2 == 0 for i in range(n)], type=pa.bool_()),
            pa.array([f"fila-{start + i}" for i in range(n)], type=pa.string()),
            # Nulos intercalados: deben sobrevivir como nulos, no como texto.
            pa.array([None if i % 2 else f"nota-{i}" for i in range(n)], type=pa.string()),
        ],
        schema=_schema(),
    )


def test_conteos_y_tipos_preservados(tmp_path):
    dest = tmp_path / "dbo" / "T.parquet"
    rows = extract_table(BatchSource(_schema(), iter([_batch(0, 3), _batch(3, 2)])), dest)
    assert rows == 5
    table = pq.read_table(dest)
    assert table.num_rows == 5
    assert pa.types.is_int64(table.schema.field("id").type)
    assert pa.types.is_decimal(table.schema.field("precio").type)
    assert pa.types.is_timestamp(table.schema.field("ts").type)
    assert pa.types.is_boolean(table.schema.field("activo").type)
    assert table.column("nota").to_pylist() == ["nota-0", None, "nota-2", "nota-0", None]


def test_zstd_y_row_groups_agrupados_por_bytes(tmp_path):
    dest = tmp_path / "T.parquet"
    extract_table(BatchSource(_schema(), iter([_batch(0, 2), _batch(2, 2), _batch(4, 1)])), dest)
    meta = pq.read_metadata(dest)
    # Contrato: los lotes se agrupan por bytes, NO un row group por lote.
    assert meta.num_row_groups == 1
    for i in range(meta.num_row_groups):
        for j in range(meta.num_columns):
            assert meta.row_group(i).column(j).compression == "ZSTD"


def test_muchos_lotes_no_multiplican_los_row_groups(tmp_path):
    """Regresión del pico de 1,9 GB: pyarrow retiene ~0,5 MB por row group.

    Con un row group por lote, una tabla de 19 M de filas y lotes de 5.000
    generaba 3.800 grupos. El número de grupos debe depender del tamaño de la
    tabla, no de cuántos lotes llegaron.
    """
    dest = tmp_path / "M.parquet"
    batches = (_batch(i * 10, 10) for i in range(500))
    extract_table(BatchSource(_schema(), batches), dest)
    meta = pq.read_metadata(dest)
    assert meta.num_rows == 5_000
    assert meta.num_row_groups <= 2


def test_tabla_vacia_con_schema_valido(tmp_path):
    dest = tmp_path / "V.parquet"
    rows = extract_table(BatchSource(_schema(), iter([])), dest)
    assert rows == 0
    table = pq.read_table(dest)  # Archivo válido aunque tenga 0 filas (S3.3).
    assert table.num_rows == 0
    assert table.schema.names == _schema().names


def test_dest_for_contrato_y_saneado(tmp_path):
    dest = dest_for(str(tmp_path), TableRef("dbo", "BSMoneda"))
    assert dest == tmp_path / "dbo" / "BSMoneda.parquet"
    raro = dest_for(str(tmp_path), TableRef("a/b", "c:d"))
    assert raro == tmp_path / "a_b" / "c_d.parquet"


@pytest.mark.parametrize("n", [1, 50_000])
def test_un_solo_batch_un_solo_grupo(tmp_path, n):
    import random

    random.seed(n)
    schema = pa.schema([("id", pa.int64())])
    batch = pa.record_batch([pa.array([random.randrange(10**9) for _ in range(n)])], schema=schema)
    dest = tmp_path / "G.parquet"
    assert extract_table(BatchSource(schema, iter([batch])), dest) == n
    assert pq.read_metadata(dest).num_row_groups == 1


# --- Costura de propiedad conexión→lector (T3-T7): el hijo se libera antes
# que el padre, en orden determinista, sin importar cuántas tablas pasen. ---

import gc as _gc  # noqa: E402


class _Tracker:
    def __init__(self):
        self.events = []


class _FakeReader:
    """Lector falso: registra su liberación (hijo)."""

    def __init__(self, tracker, schema, batches):
        self._tracker = tracker
        self.schema = schema
        self._batches = batches

    def __iter__(self):
        return iter(self._batches)

    def __del__(self):  # noqa: PYI021 — el orden de liberación es lo testeado.
        self._tracker.events.append("reader-freed")


class _FakeConnection:
    """Conexión falsa: registra creación, lectura y liberación (padre)."""

    def __init__(self, tracker):
        self._tracker = tracker
        self._tracker.events.append("conn-made")

    def read_arrow_batches(self, **kwargs):
        self._tracker.events.append("conn-read")
        schema = kwargs.get("schema") or _schema()
        batches = kwargs.get("batches") or [_batch(0, 2)]
        return _FakeReader(self._tracker, schema, batches)

    def close(self):
        self._tracker.events.append("conn-freed")


def _fake_connect_factory(tracker):
    def _connect(connection_string):
        assert connection_string.startswith("fake:")
        return _FakeConnection(tracker)
    return _connect


def test_close_libera_lector_antes_que_conexion(tmp_path):
    """El hijo (statement) se libera antes que el padre (conexión), siempre."""
    from bak2parquet.extract import odbc_batch_source

    tracker = _Tracker()
    source = odbc_batch_source(
        "fake:db", "SELECT 1", batch_size=100, max_text_size=64,
        max_binary_size=64, max_bytes_per_batch=4096,
        _connect=_fake_connect_factory(tracker))
    rows = extract_table(source, tmp_path / "T.parquet")
    assert rows == 2
    source.close()
    _gc.collect()
    assert tracker.events == ["conn-made", "conn-read", "reader-freed", "conn-freed"]


def test_muchas_tablas_no_acumulan_conexiones_ni_lectores(tmp_path):
    """200 tablas por la costura: todo lo creado se libera (neto cero)."""
    from bak2parquet.extract import odbc_batch_source

    tracker = _Tracker()
    connect = _fake_connect_factory(tracker)
    for i in range(200):
        source = odbc_batch_source(
            "fake:db", f"SELECT {i}", batch_size=100, max_text_size=64,
            max_binary_size=64, max_bytes_per_batch=4096, _connect=connect)
        assert extract_table(source, tmp_path / f"T{i}.parquet") == 2
        source.close()
    _gc.collect()
    assert tracker.events.count("conn-made") == 200
    assert tracker.events.count("conn-freed") == 200
    assert tracker.events.count("reader-freed") == 200
    # Cada lector se libera justo antes que su conexión (orden por tabla).
    for i in range(200):
        base = i * 4
        assert tracker.events[base:base + 4] == [
            "conn-made", "conn-read", "reader-freed", "conn-freed"]


def test_falla_del_lector_libera_la_conexion(tmp_path):
    """Si `read_arrow_batches` falla, la conexión no queda huérfana."""
    from bak2parquet.extract import odbc_batch_source

    tracker = _Tracker()

    class _Mala(_FakeConnection):
        def read_arrow_batches(self, **kwargs):
            raise RuntimeError("login fallido")

    def _connect(_cs):
        return _Mala(tracker)

    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="login fallido"):
        odbc_batch_source("fake:db", "SELECT 1", batch_size=1, max_text_size=1,
                          max_binary_size=1, max_bytes_per_batch=1, _connect=_connect)
    assert "conn-freed" in tracker.events
