"""Catálogo de tablas base de usuario (T5, S3.1-S3.2).

Solo control (pyodbc): lista schema + tabla, excluye vistas y tablas de
sistema. La extracción de filas vive en `extract.py` (arrow-odbc).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .restore import quote_ident

# Solo tablas base de usuario: `sys.tables` ya excluye vistas y
# `is_ms_shipped = 0` excluye las de sistema (S3.1).
CATALOG_SQL = (
    "SELECT s.name AS schema_name, t.name AS table_name "
    "FROM sys.tables AS t "
    "JOIN sys.schemas AS s ON s.schema_id = t.schema_id "
    "WHERE t.type = 'U' AND t.is_ms_shipped = 0 "
    "ORDER BY s.name, t.name;"
)


@dataclass(frozen=True)
class TableRef:
    """Identificación schema + tabla (S3.2)."""

    schema: str
    table: str

    @property
    def display(self) -> str:
        """Nombre legible `schema.tabla` para el reporte."""
        return f"{self.schema}.{self.table}"


# Tipos que arrow-odbc preasigna con `max_text_size` (texto) o
# `max_binary_size` (binario) cuando su longitud es MAX (-1). `xml` viaja
# como texto; los desconocidos con -1 se cuentan como texto (conservador).
TEXT_MAX_TYPES = frozenset({"char", "varchar", "nchar", "nvarchar", "text", "ntext", "xml"})
BINARY_MAX_TYPES = frozenset({"binary", "varbinary", "image"})


# Inventario de columnas MAX de toda la base en una sola pasada: por cada
# tabla, cuántas columnas de longitud no acotada hay de cada tipo. Sin este
# conteo el modelo de memoria (`required_memory_mb`) no puede escalar: una
# tabla con 30 VARCHAR(MAX) preasigna ~30x más tránsito que una con uno.
MAX_COLUMNS_SQL = (
    "SELECT s.name AS schema_name, t.name AS table_name, ty.name AS type_name, "
    "COUNT(*) AS n "
    "FROM sys.columns AS c "
    "JOIN sys.tables AS t ON t.object_id = c.object_id "
    "JOIN sys.schemas AS s ON s.schema_id = t.schema_id "
    "JOIN sys.types AS ty ON ty.user_type_id = c.user_type_id "
    # `text`/`ntext`/`image` son LOB legacy y en sys.columns NO tienen
    # max_length = -1: valen 16 (el puntero). Filtrar sólo por -1 los dejaba
    # fuera de la sonda, el buffer quedaba chico y 5 tablas del backup real
    # fallaban con "too large to be written into the allocated buffer".
    "WHERE t.type = 'U' AND t.is_ms_shipped = 0 "
    "AND (c.max_length = -1 OR ty.name IN ('text', 'ntext', 'image')) "
    "GROUP BY s.name, t.name, ty.name;"
)


def count_all_max_columns(conn) -> dict[tuple[str, str], tuple[int, int]]:
    """{(schema, tabla): (n_texto, n_binario)} con columnas MAX por tabla.

    Una sola consulta al catálogo (sin escanear datos): cubre las 778 tablas
    sin costo. Tablas ausentes del dict no tienen columnas MAX.
    """
    cur = conn.cursor()
    cur.execute(MAX_COLUMNS_SQL)
    counts: dict[tuple[str, str], list[int]] = {}
    for row in cur.fetchall():
        if isinstance(row, dict):
            schema, table, type_name, n = (
                row.get("schema_name"), row.get("table_name"),
                row.get("type_name"), row.get("n"),
            )
        else:
            seq = tuple(row)
            if len(seq) < 4:
                continue
            schema, table, type_name, n = seq[0], seq[1], seq[2], seq[3]
        if schema is None or table is None or type_name is None:
            continue
        key = (str(schema), str(table))
        entry = counts.setdefault(key, [0, 0])
        kind = str(type_name).strip().lower()
        if kind in BINARY_MAX_TYPES:
            entry[1] += int(n)
        else:  # texto conocido o tipo desconocido: conservador.
            entry[0] += int(n)
    return {key: (v[0], v[1]) for key, v in counts.items()}


def max_columns_for(ref: TableRef, counts: dict[tuple[str, str], tuple[int, int]]) -> tuple[int, int]:
    """(n_texto, n_binario) de columnas MAX de `ref` según el inventario."""
    return counts.get((ref.schema, ref.table), (0, 0))


# Nombres y tipos de las columnas MAX de una tabla (para la sonda DATALENGTH).
def max_column_names_sql(db_name: str) -> str:
    """Plantilla de columnas MAX, calificada con la base.

    Corre sobre la conexión de CONTROL, que vive en `master`: sin calificar, la
    tabla no aparece y los techos de buffer nunca se ajustan.
    """
    db = db_name.replace("]", "]]")
    return (
    "SELECT c.name AS column_name, ty.name AS type_name "
    f"FROM [{db}].[sys].[columns] AS c "
    f"JOIN [{db}].[sys].[tables] AS t ON t.object_id = c.object_id "
    f"JOIN [{db}].[sys].[schemas] AS s ON s.schema_id = t.schema_id "
    f"JOIN [{db}].[sys].[types] AS ty ON ty.user_type_id = c.user_type_id "
    "WHERE t.type = 'U' AND t.is_ms_shipped = 0 "
    "AND (c.max_length = -1 OR ty.name IN ('text', 'ntext', 'image')) "
    "AND s.name = {schema} AND t.name = {table} "
    "ORDER BY c.column_id;"
    )


def max_column_names(
    conn, ref: TableRef, db_name: str
) -> list[tuple[str, str]]:
    """[(columna, 'text'|'binary)] con longitud MAX de `ref` (vacía si no hay)."""
    from .restore import quote_literal

    cur = conn.cursor()
    cur.execute(max_column_names_sql(db_name).format(
        schema=quote_literal(ref.schema), table=quote_literal(ref.table)))
    names: list[tuple[str, str]] = []
    for row in cur.fetchall():
        if isinstance(row, dict):
            name, type_name = row.get("column_name"), row.get("type_name")
        else:
            seq = tuple(row)
            if len(seq) < 2:
                continue
            name, type_name = seq[0], seq[1]
        if name is None:
            continue
        kind = "binary" if str(type_name).strip().lower() in BINARY_MAX_TYPES else "text"
        names.append((str(name), kind))
    return names


def probe_max_lengths_sql(db_name: str, ref: TableRef, columns: list[tuple[str, str]]) -> str:
    """Un solo `SELECT MAX(DATALENGTH(col))` por columna MAX (un único scan).

    Solo se usa cuando los techos por defecto no entran en el buffer: el
    backup restaurado es una foto estática (nadie escribe), así que el máximo
    observado ES el máximo real y achicar los techos a ese valor es seguro.
    """
    target = f"{quote_ident(db_name)}.{quote_ident(ref.schema)}.{quote_ident(ref.table)}"
    selects = ", ".join(f"MAX(DATALENGTH({quote_ident(name)}))" for name, _kind in columns)
    return f"SELECT {selects} FROM {target} WITH (NOLOCK);"


def rows_to_tables(rows: Iterable) -> list[TableRef]:
    """Normaliza filas del catálogo (mappings o tuplas) a `TableRef`."""
    tables: list[TableRef] = []
    for row in rows:
        if isinstance(row, dict):
            schema, table = row.get("schema_name"), row.get("table_name")
        else:
            seq = tuple(row)
            if len(seq) < 2:
                continue
            schema, table = seq[0], seq[1]
        if schema is None or table is None:
            continue
        tables.append(TableRef(schema=str(schema), table=str(table)))
    return tables


def list_tables(conn) -> list[TableRef]:
    """Lista las tablas base de usuario de la base a la que apunta `conn`."""
    cur = conn.cursor()
    cur.execute(CATALOG_SQL)
    return rows_to_tables(cur.fetchall())


def table_query(ref: TableRef) -> str:
    """Lectura de una tabla sin bloquear el motor ni abrir transacciones (S4.1)."""
    return f"SELECT * FROM {quote_ident(ref.schema)}.{quote_ident(ref.table)} WITH (NOLOCK);"
