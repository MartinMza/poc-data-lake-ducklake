"""Extracción en streaming columnar a Parquet ZSTD (T6, S3.3, S4).

Transferencia estrictamente en lotes con `arrow-odbc` (columnar batches) →
`pyarrow`. PROHIBIDO: pandas, sqlalchemy, objetos Python por fila.

Huella plana (< 250 MB): el `ParquetWriter` escribe cada batch al llegar
(un row-group por batch, sin reescribir) y `max_text_size`/`max_binary_size`
acotan los buffers de las columnas MAX.

Testabilidad sin motor: `BatchSource` es la costura inyectable — los tests
alimentan `RecordBatches` sintéticos y afirman conteos, compresión ZSTD,
tipos preservados y tabla vacía con schema válido.
"""

from __future__ import annotations

import gc
import os
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .catalog import TableRef
from .config import sanitize_filename
from .mem import check_rss_budget, rss_mb

if TYPE_CHECKING:  # Solo para tipos: pyarrow se importa tarde (ver `--help` sin deps).
    import pyarrow as pa

DEFAULT_COMPRESSION = "zstd"

# Traza por lote (opt-in): distingue memoria nativa de Arrow del resto del
# proceso cuando se investiga una fuga dentro de una tabla grande.
_DEBUG_BATCHES = os.environ.get("BAK2PARQUET_DEBUG_BATCHES") == "1"

# Tope de datos acumulados por row group. Acota la memoria del writer y, sobre
# todo, el número de row groups (y sus estadísticas) para tablas enormes.
ROW_GROUP_TARGET_BYTES = 64 * 1024 * 1024


@dataclass
class BatchSource:
    """Costura inyectable: schema conocido + iterable de batches columnares.

    En el camino real (`odbc_batch_source`) también retiene la conexión ODBC
    que le dio vida al lector (`_connection`). El lector de arrow-odbc es un
    statement hijo de esa conexión: la conexión debe liberarse DESPUÉS que el
    lector (padre después del hijo). `read_arrow_batches_from_odbc` no lo
    garantiza — su `connection` local muere al retornar, huérfano del lector,
    y cada tabla deja estado del driver sin liberar en el proceso. Por eso el
    lector se crea acá con `connect()` explícito y `close()` libera en orden:
    primero el lector, `gc.collect()` para que el Drop de Rust corra con el
    padre aún vivo, y recién después la conexión.
    """

    schema: pa.Schema  # type: ignore[name-defined]
    batches: Iterable[pa.RecordBatch]  # type: ignore[name-defined]
    _connection: Any = field(default=None, repr=False)

    def close(self) -> None:
        """Libera lector y conexión en orden (hijo antes que el padre)."""
        batches, self.batches = self.batches, iter(())
        connection, self._connection = self._connection, None
        del batches
        gc.collect()  # el lector de Rust se libera con la conexión aún viva.
        if connection is None:
            return
        closer = getattr(connection, "close", None)
        if callable(closer):
            closer()
        else:
            del connection
        gc.collect()


def odbc_batch_source(
    connection_string: str,
    query: str,
    *,
    batch_size: int,
    max_text_size: int,
    max_binary_size: int,
    max_bytes_per_batch: int,
    _connect: Callable[..., Any] | None = None,
) -> BatchSource:
    """Fuente real: lotes Arrow directo del driver ODBC (S4.2, S4.4).

    `fetch_concurrently=False` a propósito: el doble buffer de tránsito
    duplica la memoria y no entra en el presupuesto del contenedor efímero.
    Import tardío para que el resto del paquete no exija el driver.
    `_connect` es la costura de tests (default: `arrow_odbc.connect`).
    """
    if _connect is None:
        from arrow_odbc import connect as _connect  # type: ignore[assignment]

    connection = _connect(connection_string)
    try:
        reader = connection.read_arrow_batches(
            query=query,
            batch_size=batch_size,
            max_text_size=max_text_size,
            max_binary_size=max_binary_size,
            max_bytes_per_batch=max_bytes_per_batch,
            fetch_concurrently=False,
        )
    except Exception:
        closer = getattr(connection, "close", None)
        if callable(closer):
            closer()
        else:
            del connection
        raise
    return BatchSource(schema=reader.schema, batches=reader, _connection=connection)


def dest_for(out_root: str, ref: TableRef) -> Path:
    """Ruta de salida congelada: `<out>/<schema>/<table>.parquet` (contrato)."""
    return Path(out_root) / sanitize_filename(ref.schema) / (sanitize_filename(ref.table) + ".parquet")


def extract_table(
    source: BatchSource,
    dest: Path,
    *,
    compression: str = DEFAULT_COMPRESSION,
    memory_limit_mb: int = 0,
    context: str = "",
) -> int:
    """Escribe los batches en Parquet ZSTD, uno por vez. Devuelve las filas.

    Tabla vacía (S3.3): con cero batches igual escribe un Parquet válido con
    el schema, porque el schema viaja en la fuente, no en los datos.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    dest.parent.mkdir(parents=True, exist_ok=True)
    writer = None
    rows = 0
    batches = 0
    try:
        # Los batches se agrupan por BYTES antes de volcarlos, no uno por row
        # group. pyarrow retiene ~0,5 MB de estadísticas por row group: con una
        # tabla de 19 M de filas y lotes de 5.000 eso daba 3.800 grupos y 1,9 GB
        # de RSS. Agrupando por bytes, el número de grupos depende del tamaño de
        # la tabla (no del lote) y la memoria queda acotada por el objetivo.
        pendientes: list[Any] = []
        pendientes_bytes = 0
        for batch in source.batches:
            pendientes.append(batch)
            pendientes_bytes += batch.nbytes
            rows += batch.num_rows
            if pendientes_bytes >= ROW_GROUP_TARGET_BYTES:
                if writer is None:
                    writer = pq.ParquetWriter(dest, source.schema, compression=compression)
                writer.write_table(pa.Table.from_batches(pendientes))
                pendientes = []
                pendientes_bytes = 0
            batches += 1
            # Traza de diagnóstico (opt-in): separa memoria nativa de Arrow del
            # resto del proceso. Sin esto, una fuga dentro de una tabla es
            # indistinguible de buffers del driver.
            if _DEBUG_BATCHES and batches % 20 == 0:
                import pyarrow as _pa

                print(
                    f"  [debug] lote {batches} rss={rss_mb():.0f}MB "
                    f"arrow={_pa.total_allocated_bytes() / 1048576:.0f}MB "
                    f"filas={rows}",
                    file=sys.stderr,
                )
            # La guardia corre por lote, no por tabla: entre tablas el daño ya
            # está hecho y el OOM-killer del contenedor gana la carrera.
            if memory_limit_mb:
                check_rss_budget(memory_limit_mb, context=context or dest.name)
        if writer is None:
            writer = pq.ParquetWriter(dest, source.schema, compression=compression)
        if pendientes:
            writer.write_table(pa.Table.from_batches(pendientes))
    finally:
        if writer is not None:
            writer.close()
    return rows
