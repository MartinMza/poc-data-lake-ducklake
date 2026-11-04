"""bak2parquet — extractor efímero `.bak` (SQL Server) → Parquet ZSTD.

Restaura un backup en un motor SQL Server efímero con memoria acotada (S1),
descubre sus tablas base (S3) y las exporta en streaming columnar con
`arrow-odbc` → Parquet ZSTD (S4), con higiene de memoria entre tablas (S5).

Este `__init__` no importa dependencias de terceros a propósito: `python -m
bak2parquet --help` debe funcionar con el Python del sistema.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
