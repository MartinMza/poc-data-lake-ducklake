"""Tests sin motor: `run()` escribe `report.json` en TODOS los caminos (T3-T7).

S5.3 es inverificable si un crash o una falla temprana no deja reporte: cada
falla (restore, catálogo, tabla, presupuesto) debe dejar `report.json` con lo
completado, las filas, los segundos por tabla y el error.
"""

import json

import pytest

import bak2parquet.cli as cli
from bak2parquet.catalog import TableRef
from bak2parquet.config import load_config
from bak2parquet.restore import RestoreError


class _Cursor:
    def __init__(self, owner):
        self._owner = owner
        self._last = ""

    def execute(self, sql):
        self._owner.seen.append(sql)
        if self._owner.fail_on and self._owner.fail_on in sql:
            raise RuntimeError("boom del motor")
        self._last = sql

    def fetchall(self):
        if "create_date" in self._last:  # reaper: sin huérfanas.
            return []
        if "COUNT" in self._last and "sys.columns" in self._last:
            return list(self._owner.max_count_rows)
        if "[sys].[columns]" in self._last or "sys.columns" in self._last:
            # Con la sonda incondicional, este doble tiene que poder decir que
            # una tabla NO tiene columnas MAX (si no, todas la tendrían).
            only = getattr(self._owner, "max_name_only_for", None)
            if only is not None and only not in self._last:
                return []
            return list(self._owner.max_name_rows)
        if "sys.tables" in self._last:
            return list(self._owner.catalog_rows)
        return []

    def fetchone(self):
        if "state_desc" in self._last:
            return ("ONLINE",)
        if "DATALENGTH" in self._last:
            return tuple(self._owner.probe_row)
        raise AssertionError(f"fetchone inesperado tras: {self._last!r}")

    def nextset(self):
        return False


class _Master:
    def __init__(self, catalog_rows=(), max_count_rows=(), max_name_rows=(),
                 probe_row=(), fail_on=None):
        self.seen = []
        self.closed = False
        self.catalog_rows = catalog_rows
        self.max_count_rows = max_count_rows
        self.max_name_rows = max_name_rows
        self.probe_row = probe_row
        self.fail_on = fail_on

    def cursor(self):
        return _Cursor(self)

    def close(self):
        self.closed = True


class _Source:
    def __init__(self, owner):
        self._owner = owner
        self.closed = False

    def close(self):
        self.closed = True
        self._owner["closed_sources"] += 1


class _Engine:
    def __init__(self, cfg):
        self._cfg = cfg
        self.bak_path = cfg.bak_path or cfg.bak
        self.stopped = False
        self.started = False

    def connection_string(self, database="master"):
        return f"fake:{database}"

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def ctx(monkeypatch, tmp_path):
    """Mundo falso: motor, control y extracción por la costura."""
    bak = tmp_path / "f.bak"
    bak.write_bytes(b"x")
    out = tmp_path / "out"
    report_json = tmp_path / "report.json"
    master = _Master()
    state = {"extract_calls": [], "closed_sources": 0,
             "fail_tables": set(), "restore_exc": None}

    monkeypatch.setattr(cli, "UrlEngine", _Engine)
    monkeypatch.setattr(cli, "DockerEngine", _Engine)
    monkeypatch.setattr(cli, "connect_control", lambda cs, timeout_sec=30: master)

    def _restore(conn, db_name, container_bak):
        state["restore_calls"] = getattr(state, "restore_calls", 0) + 1
        if state["restore_exc"] is not None:
            raise state["restore_exc"]

    def _source(db_conn_str, query, **kw):
        src = _Source(state)
        state["extract_calls"].append(query)
        return src

    def _extract(source, dest, **_kwargs):
        for q in state["extract_calls"][-1:]:
            for bad in state["fail_tables"]:
                if bad in q:
                    raise RuntimeError(f"columna rara en {bad}")
        return 7

    monkeypatch.setattr(cli, "restore_database", _restore)
    monkeypatch.setattr(cli, "odbc_batch_source", _source)
    monkeypatch.setattr(cli, "extract_table", _extract)
    return bak, out, report_json, master, state


def _cfg(bak, out, report_json, extra=None):
    env = {"MSSQL_SA_PASSWORD": "x"}
    overrides = {"bak": str(bak), "out": str(out), "engine": "url",
                 "mssql_host": "h", "report_json": str(report_json)}
    overrides.update(extra or {})
    return load_config(overrides, env=env)


def _read(report_json):
    return json.loads(report_json.read_text(encoding="utf-8"))


def test_restore_fallido_escribe_reporte_con_motivo(ctx, capsys):
    bak, out, report_json, master, state = ctx
    state["restore_exc"] = RestoreError("Operating system error 112 (sin espacio)")
    code = cli.run(_cfg(bak, out, report_json))
    assert code == 1
    datos = _read(report_json)
    assert datos["detected"] == 0 and datos["failed"] == 0
    assert "112" in datos["fatal_error"] and "restauración fallida" in datos["fatal_error"]
    assert "error fatal" in capsys.readouterr().out  # el humano también sale.
    assert master.closed  # teardown: se cierra el control.


def test_catalogo_fallido_escribe_reporte(ctx):
    bak, out, report_json, master, state = ctx
    master.fail_on = "sys.tables"
    code = cli.run(_cfg(bak, out, report_json))
    assert code == 1
    datos = _read(report_json)
    assert "catálogo" in datos["fatal_error"]


def test_error_por_tabla_registra_y_sigue_con_exit_1(ctx):
    bak, out, report_json, master, state = ctx
    master.catalog_rows = [("dbo", "A"), ("dbo", "B"), ("dbo", "C")]
    state["fail_tables"] = {"B"}
    code = cli.run(_cfg(bak, out, report_json))
    assert code == 1
    datos = _read(report_json)
    assert datos["detected"] == 3 and datos["exported_ok"] == 2 and datos["failed"] == 1
    assert datos["fatal_error"] is None
    assert datos["tables"][1]["error"] == "columna rara en B"
    assert all(t["seconds"] >= 0 for t in datos["tables"])  # segundos por tabla.
    assert state["closed_sources"] == 3  # cada lector se cerró en orden.


def test_tabla_ancha_usa_techos_ajustados_y_sale_0(ctx):
    """30 VARCHAR(MAX) no entran con techos default: sonda + reintento → OK."""
    bak, out, report_json, master, state = ctx
    master.catalog_rows = [("dbo", "Ancha")]
    master.max_count_rows = [("dbo", "Ancha", "varchar", 30)]
    master.max_name_rows = [(f"c{i}", "varchar") for i in range(30)]
    master.probe_row = tuple([1000] * 30)  # 1 KB real por columna.
    code = cli.run(_cfg(bak, out, report_json))
    assert code == 0
    datos = _read(report_json)
    assert datos["exported_ok"] == 1 and datos["failed"] == 0
    assert any("DATALENGTH" in s for s in master.seen)  # la sonda corrió.


def test_tabla_imposible_queda_como_error_y_no_aborta(ctx):
    """Ni ajustada entra (límite 250 MB): error de ESA tabla, se sigue."""
    bak, out, report_json, master, state = ctx
    master.catalog_rows = [("dbo", "Ancha"), ("dbo", "B")]
    master.max_count_rows = [("dbo", "Ancha", "varchar", 30)]
    master.max_name_rows = [(f"c{i}", "varchar") for i in range(30)]
    master.max_name_only_for = "Ancha"  # B no tiene columnas MAX.
    master.probe_row = tuple([4_000_000] * 30)  # 4 MB reales por columna.
    code = cli.run(_cfg(bak, out, report_json, {"memory_limit_mb": 250}))
    assert code == 1
    datos = _read(report_json)
    assert datos["fatal_error"] is None  # no es fatal: B sí se exportó.
    assert datos["exported_ok"] == 1 and datos["failed"] == 1
    assert "presupuesto" in datos["tables"][0]["error"]


def test_rss_sobre_limite_aborta_limpio_con_reporte(ctx, monkeypatch):
    """RSS medido sobre el techo: fatal limpio (no SIGSEGV/OOM), con reporte."""
    bak, out, report_json, master, state = ctx
    master.catalog_rows = [("dbo", "A"), ("dbo", "B")]
    monkeypatch.setattr(cli, "check_rss_budget",
                        lambda limit, context: (_ for _ in ()).throw(
                            cli.MemoryBudgetExceeded("presupuesto de memoria agotado en test")))
    code = cli.run(_cfg(bak, out, report_json))
    assert code == 1
    datos = _read(report_json)
    assert "presupuesto" in datos["fatal_error"]
    assert datos["failed"] == 1 and datos["exported_ok"] == 0


def test_exito_escribe_reporte_y_exit_0(ctx):
    bak, out, report_json, master, state = ctx
    master.catalog_rows = [("dbo", "A"), ("dbo", "B")]
    code = cli.run(_cfg(bak, out, report_json))
    assert code == 0
    datos = _read(report_json)
    assert datos["detected"] == 2 and datos["total_rows"] == 14
    assert datos["fatal_error"] is None and datos["elapsed_sec"] is not None
