"""Integración con `mssql-backup/test-source.bak` (T9).

Solo corre con `BAK2PARQUET_IT=1` y un motor disponible: levanta el motor
efímero en modo `docker`, restaura el .bak de prueba y verifica un Parquet
por tabla. Requiere Docker y `MSSQL_SA_PASSWORD` en el entorno.
"""

import os

import pytest

pytestmark = pytest.mark.integration

if os.environ.get("BAK2PARQUET_IT") != "1":
    pytest.skip("integración con motor: solo corre con BAK2PARQUET_IT=1.", allow_module_level=True)

from pathlib import Path  # noqa: E402

from bak2parquet.cli import main  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_BAK = REPO_ROOT / "mssql-backup" / "test-source.bak"


def test_bak_de_prueba_a_parquet(tmp_path):
    assert TEST_BAK.is_file(), f"falta el .bak de prueba: {TEST_BAK}"
    out = tmp_path / "parquet"
    code = main([
        "--bak", str(TEST_BAK),
        "--out", str(out),
        "--engine", "docker",
        "--batch-size", "1000",
        "--report-json", str(tmp_path / "report.json"),
    ])
    assert code == 0
    parquets = sorted(out.rglob("*.parquet"))
    assert parquets, "el .bak de prueba no produjo ningún Parquet"

    import pyarrow.parquet as pq

    for parquet in parquets:
        meta = pq.read_metadata(parquet)
        assert meta.num_rows >= 0
        for i in range(meta.num_row_groups):
            for j in range(meta.num_columns):
                assert meta.row_group(i).column(j).compression == "ZSTD"


def test_restore_deja_la_base_online_en_motor_real():
    """Nivel integración: `restore_database` deja la base ONLINE (T4).

    Corre contra una instancia existente (modo `url`): necesita
    `MSSQL_SA_PASSWORD` y que el motor vea el .bak en
    `BAK2PARQUET_IT_BAK_PATH` (default: la ruta del contenedor dulse-mssql).
    Sin password se omite, igual que el resto del módulo sin BAK2PARQUET_IT.
    """
    import pytest as _pytest

    password = os.environ.get("MSSQL_SA_PASSWORD", "")
    if not password:
        _pytest.skip("sin MSSQL_SA_PASSWORD no hay motor real contra el que probar.")
    try:
        import pyodbc
    except ImportError:
        _pytest.skip("sin pyodbc no hay prueba contra motor real.")

    from bak2parquet.config import odbc_connection_string
    from bak2parquet.engine import generate_database_name
    from bak2parquet.hygiene import drop_database
    from bak2parquet.restore import database_state, restore_database

    host = os.environ.get("BAK2PARQUET_IT_HOST", "localhost")
    port = int(os.environ.get("BAK2PARQUET_IT_PORT", "1433"))
    user = os.environ.get("BAK2PARQUET_IT_USER", "sa")
    bak_path = os.environ.get(
        "BAK2PARQUET_IT_BAK_PATH", "/var/opt/mssql/backup/test-source.bak")
    conn = pyodbc.connect(
        odbc_connection_string(host=host, port=port, user=user,
                               password=password, database="master"),
        timeout=30, autocommit=True,
    )
    db_name = generate_database_name()
    try:
        restore_database(conn, db_name=db_name, container_bak=bak_path)
        assert database_state(conn, db_name) == "ONLINE"
    finally:
        drop_database(conn, db_name, owned={db_name})
        assert database_state(conn, db_name) is None
        conn.close()
