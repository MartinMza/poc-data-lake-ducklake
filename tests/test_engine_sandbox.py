"""Guardrails del sandbox SQL (`engine.validate_sql`).

Cubre que una query de solo lectura siga pasando (contrato vigente) y que los
vectores de fuga —funciones que leen archivos y URLs externas— queden cerrados,
porque la denylist de keywords de escritura no los cubría (robo de `.env`/
credenciales por `read_text`/`read_csv`, SSRF por `http(s)://`, cross-bucket por
`s3://`).
"""

from __future__ import annotations

import pytest

from dulse.adapters.outbound import duckdb_engine as engine


# --- contrato vigente: lo que ya funcionaba debe seguir funcionando ----------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM lake.bronze.mssql_bs_moneda LIMIT 5",
        "WITH t AS (SELECT 1 AS n) SELECT n FROM t",
        "DESCRIBE lake.bronze.mssql_bs_producto",
        "EXPLAIN SELECT 1",
        "SELECT * FROM lake.bronze.mssql_bs_moneda AT (VERSION => 144)",
    ],
)
def test_lecturas_validas_pasan(sql: str) -> None:
    assert engine.validate_sql(sql) == sql.strip()


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE lake.bronze.mssql_bs_moneda",
        "DELETE FROM lake.bronze.mssql_bs_moneda",
        "INSERT INTO x VALUES (1)",
        "COPY lake.bronze.mssql_bs_moneda TO '/tmp/x.csv'",
        "ATTACH 'x.db'",
        "SET disabled_filesystems=''",
        "SELECT 1; SELECT 2",
        "   ",
    ],
)
def test_escritura_y_admin_siguen_bloqueadas(sql: str) -> None:
    with pytest.raises(ValueError):
        engine.validate_sql(sql)


# --- vectores nuevos a cerrar (RED) ------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT content FROM read_text('/app/.env')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('/tmp/x.parquet')",
        "SELECT * FROM read_blob('/etc/shadow')",
        "SELECT * FROM read_json('/tmp/x.json')",
        "SELECT * FROM glob('/**')",
        "SELECT * FROM parquet_metadata('/tmp/x.parquet')",
        "SELECT * FROM duckdb_secrets()",
        "SELECT which_secret('s3://x', 's3')",
        "SELECT read_text ('/app/.env')",  # espacio antes del paréntesis
        "select * from READ_CSV('/etc/passwd')",  # case-insensitive
    ],
)
def test_funciones_lectoras_de_archivos_rechazadas(sql: str) -> None:
    with pytest.raises(ValueError):
        engine.validate_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('http://169.254.169.254/latest/meta-data/')",
        "SELECT * FROM 'https://evil.example/x.csv'",
        "SELECT * FROM read_parquet('s3://otro-bucket/x.parquet')",
        "SELECT * FROM read_csv('HTTP://EVIL/x')",
    ],
)
def test_urls_externas_rechazadas(sql: str) -> None:
    with pytest.raises(ValueError):
        engine.validate_sql(sql)
