"""Tests sin motor: config fail-closed, entorno/CLI y saneado (T2)."""

import pytest

from bak2parquet.config import (
    ConfigError,
    load_config,
    odbc_connection_string,
    sanitize_filename,
)


def _env(*, bak, password="secreta", extra=None):
    env = {"MSSQL_SA_PASSWORD": password}
    env.update(extra or {})
    return env


def test_minima_valida(tmp_path):
    bak = tmp_path / "fuente.bak"
    bak.write_bytes(b"falso")
    cfg = load_config({"bak": str(bak), "out": str(tmp_path / "out")}, env=_env(bak=bak))
    assert cfg.batch_size == 50_000
    assert cfg.engine == "docker"
    assert cfg.mssql_password == "secreta"


def test_falta_bak(tmp_path):
    with pytest.raises(ConfigError):
        load_config({"out": str(tmp_path)}, env=_env(bak=None))


def test_bak_inexistente(tmp_path):
    with pytest.raises(ConfigError):
        load_config({"bak": str(tmp_path / "nope.bak")}, env=_env(bak=None))


def test_bak_no_bak(tmp_path):
    otro = tmp_path / "fuente.tar"
    otro.write_bytes(b"x")
    with pytest.raises(ConfigError):
        load_config({"bak": str(otro), "out": str(tmp_path)}, env=_env(bak=None))


def test_falta_out(tmp_path):
    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    with pytest.raises(ConfigError):
        load_config({"bak": str(bak)}, env=_env(bak=None))


@pytest.mark.parametrize("campo", ["batch_size", "max_text_size", "max_binary_size", "max_bytes_per_batch"])
@pytest.mark.parametrize("valor", [0, -5])
def test_enteros_positivos(tmp_path, campo, valor):
    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    with pytest.raises(ConfigError):
        load_config({"bak": str(bak), "out": "o", campo: valor}, env=_env(bak=None))


def test_env_no_entero_es_fail_closed(tmp_path):
    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    with pytest.raises(ConfigError):
        load_config({"bak": str(bak), "out": "o"},
                    env=_env(bak=None, extra={"BAK2PARQUET_BATCH_SIZE": "muchas"}))


def test_engine_desconocido(tmp_path):
    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    with pytest.raises(ConfigError):
        load_config({"bak": str(bak), "out": "o", "engine": "k8s"}, env=_env(bak=None))


def test_password_vacia_es_fail_closed(tmp_path):
    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    with pytest.raises(ConfigError):
        load_config({"bak": str(bak), "out": "o"}, env={})


def test_env_y_precedencia_cli(tmp_path):
    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    env = _env(bak=None, extra={"BAK2PARQUET_BATCH_SIZE": "1000", "BAK2PARQUET_ENGINE": "url",
                                "MSSQL_HOST": "mssql"})
    cfg = load_config({"bak": str(bak), "out": "o"}, env=env)
    assert cfg.batch_size == 1000
    assert cfg.engine == "url"
    assert cfg.mssql_host == "mssql"
    # La CLI pisa al entorno.
    cfg2 = load_config({"bak": str(bak), "out": "o", "batch_size": 7}, env=env)
    assert cfg2.batch_size == 7


def test_sanitize_filename():
    assert sanitize_filename("dbo") == "dbo"
    assert "/" not in sanitize_filename("a/b")
    assert "\\" not in sanitize_filename("a\\b")
    assert ":" not in sanitize_filename("C:tabla")
    assert "\x00" not in sanitize_filename("a\x00b")
    assert sanitize_filename("   ") == "_"


def test_connection_string_escapa_password():
    conn = odbc_connection_string(host="h", port=1433, user="sa",
                                  password="a;b}c", database="master")
    assert "Driver={ODBC Driver 18 for SQL Server}" in conn
    assert "TrustServerCertificate=yes" in conn
    assert "PWD={a;b}}c}" in conn
    assert "secreta" not in conn  # no fuga de otros valores


def test_los_defaults_entran_en_el_techo_de_memoria(tmp_path):
    """El techo por defecto debe poder cumplirse: si no, muere con SIGSEGV."""
    from bak2parquet.config import required_memory_mb

    bak = tmp_path / "fuente.bak"
    bak.write_bytes(b"falso")
    cfg = load_config({"bak": str(bak), "out": str(tmp_path / "out")}, env=_env(bak=bak))
    assert required_memory_mb(cfg) <= cfg.memory_limit_mb


def test_lote_demasiado_grande_para_el_techo_es_rechazado(tmp_path):
    """128 MiB de lote no entran en 300 MB: se rechaza en vez de reventar."""
    bak = tmp_path / "fuente.bak"
    bak.write_bytes(b"falso")
    with pytest.raises(ConfigError, match="no alcanza"):
        load_config(
            {
                "bak": str(bak),
                "out": str(tmp_path / "out"),
                "max_bytes_per_batch": 134_217_728,
            },
            env=_env(bak=bak),
        )


def test_limite_desactivado_no_impone_techo(tmp_path):
    bak = tmp_path / "fuente.bak"
    bak.write_bytes(b"falso")
    cfg = load_config(
        {
            "bak": str(bak),
            "out": str(tmp_path / "out"),
            "max_bytes_per_batch": 134_217_728,
            "memory_limit_mb": 0,
        },
        env=_env(bak=bak),
    )
    assert cfg.memory_limit_mb == 0


def test_required_memory_escala_con_columnas_max(tmp_path):
    """El modelo crece con cada columna MAX (31 VARCHAR(MAX) >> 1)."""
    from bak2parquet.config import required_memory_mb

    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    cfg = load_config({"bak": str(bak), "out": "o"}, env=_env(bak=None))
    base = required_memory_mb(cfg)
    # El costo por columna se deriva del techo configurado, no de un número
    # cableado: si el default cambia, el test sigue midiendo lo mismo.
    por_columna = max(1, -(-cfg.max_text_size // (1024 * 1024)))
    assert required_memory_mb(cfg, n_max_text=5) - base == 4 * por_columna
    por_binario = max(1, -(-cfg.max_binary_size // (1024 * 1024)))
    assert required_memory_mb(cfg, n_max_binary=3) - base == 2 * por_binario
    # Sin columnas MAX el piso es el tránsito + overhead.
    assert required_memory_mb(cfg, n_max_text=0, n_max_binary=0) == 96 + 3 * 48


def test_presupuesto_imposible_se_rechaza_en_config(tmp_path):
    """Un techo que ni el piso cumple se rechaza antes de tocar el motor."""
    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    with pytest.raises(ConfigError, match="no alcanza"):
        load_config({"bak": str(bak), "out": "o", "memory_limit_mb": 100},
                    env=_env(bak=None))


def test_reap_max_age_sec_default_y_validacion(tmp_path):
    """El reaper viene activo (1h) y rechaza edades negativas."""
    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    cfg = load_config({"bak": str(bak), "out": "o"}, env=_env(bak=None))
    assert cfg.reap_max_age_sec == 3600
    cfg2 = load_config({"bak": str(bak), "out": "o", "reap_max_age_sec": 0},
                       env=_env(bak=None))
    assert cfg2.reap_max_age_sec == 0  # 0 desactiva el reaper.
    with pytest.raises(ConfigError, match="reap-max-age-sec"):
        load_config({"bak": str(bak), "out": "o", "reap_max_age_sec": -1},
                    env=_env(bak=None))
