"""CLI y orquestación del extractor (T2, S6.3).

Contrato: `python -m bak2parquet --bak <ruta> --out <dir> [--batch-size N]
[--max-text-size N] [--max-binary-size N] [--report-json <ruta>]`.
Exit 0 solo si todas las tablas se exportaron; si no, el motivo del motor
va a stderr. La limpieza (DROP + teardown) corre en `finally` (S5.2).
"""

from __future__ import annotations

import argparse
import gc
import os
import sys
import time
from typing import Sequence

from .catalog import (
    TableRef,
    count_all_max_columns,
    list_tables,
    max_column_names,
    max_columns_for,
    probe_max_lengths_sql,
    table_query,
)
from .config import (
    DEFAULT_IMAGE,
    DEFAULT_MAX_TEXT_SIZE,
    Config,
    ConfigError,
    load_config,
    required_memory_mb,
)
from .engine import DockerEngine, EngineError, UrlEngine, connect_control, generate_database_name
from .extract import dest_for, extract_table, odbc_batch_source
from .hygiene import RefusedDropError, drop_database, purge_buffers, reap_stale_databases
from .mem import MemoryBudgetExceeded, check_rss_budget, rss_mb
from .report import RunReport, TableResult, render_human
from .restore import RestoreError, restore_database


def build_parser() -> argparse.ArgumentParser:
    """Argumentos CLI (contrato de salida + modos de motor + debug)."""
    parser = argparse.ArgumentParser(
        prog="bak2parquet",
        description="Extractor efímero .bak (SQL Server) → Parquet ZSTD.",
    )
    parser.add_argument("--bak", default=None, help="ruta del .bak (o BAK2PARQUET_BAK).")
    parser.add_argument("--out", default=None, help="directorio de salida Parquet (o BAK2PARQUET_OUT).")
    parser.add_argument("--batch-size", type=int, default=None, help="filas por batch columnar (default 50000).")
    parser.add_argument("--max-text-size", type=int, default=None,
                        help="techo de buffers VARCHAR(MAX) en bytes (default 8 MiB).")
    parser.add_argument("--max-binary-size", type=int, default=None,
                        help="techo de buffers VARBINARY(MAX) en bytes (default 8 MiB).")
    parser.add_argument("--max-bytes-per-batch", type=int, default=None,
                        help="techo del buffer de tránsito arrow-odbc en bytes (default 128 MiB).")
    parser.add_argument("--report-json", default=None, help="ruta del resumen JSON (o BAK2PARQUET_REPORT_JSON).")
    parser.add_argument("--engine", choices=("docker", "url"), default=None,
                        help="docker: contenedor efímero (default); url: instancia existente.")
    parser.add_argument("--keep-engine", action="store_true",
                        help="debug (docker): no eliminar el contenedor al terminar.")
    parser.add_argument("--keep-db", action="store_true",
                        help="debug: no hacer DROP DATABASE de la base temporal.")
    parser.add_argument("--bak-path", default=None,
                        help="ruta del .bak vista por el motor en modo url (default: --bak).")
    parser.add_argument("--mssql-host", default=None, help="host SQL (modo url; o MSSQL_HOST).")
    parser.add_argument("--mssql-port", type=int, default=None, help="puerto SQL (o MSSQL_PORT).")
    parser.add_argument("--mssql-user", default=None, help="usuario SQL (o MSSQL_USER).")
    parser.add_argument("--mssql-password", default=None,
                        help="password SQL (o MSSQL_SA_PASSWORD). Nunca se loguea.")
    parser.add_argument("--image", default=None, help=f"imagen del motor efímero (default {DEFAULT_IMAGE}).")
    parser.add_argument("--memory-limit-mb", type=int, default=None,
                        help="presupuesto RSS de Python en MB; 0 lo desactiva (default 300). "
                        "Al superarlo la corrida aborta con un error limpio (no SIGSEGV/OOM).")
    parser.add_argument("--reap-max-age-sec", type=int, default=None,
                        help="edad mínima para reap de bases bak2pq_* huérfanas al arrancar "
                        "(default 3600s; 0 desactiva el reaper).")
    parser.add_argument("--startup-timeout-sec", type=int, default=None,
                        help="timeout de readiness del motor en segundos (default 180).")
    return parser


def config_from_parsed(args: argparse.Namespace) -> Config:
    """Convierte el namespace de argparse a `Config` validada (T2)."""
    overrides = {k: v for k, v in vars(args).items() if v is not None}
    # Los flags store_true en False no pisan al entorno (que sí puede activarlos).
    for flag in ("keep_engine", "keep_db"):
        if overrides.get(flag) is False:
            del overrides[flag]
    return load_config(overrides)


class _Fatal(RuntimeError):
    """Falla de corrida ya informada: solo falta report + teardown + exit != 0."""


class _TableTooBig(RuntimeError):
    """La tabla no entra en el presupuesto ni con techos ajustados.

    Es un error POR TABLA (se registra y se sigue con la siguiente), no fatal:
    el resto de las tablas sí puede exportarse dentro del presupuesto.
    """


# Piso para los techos ajustados por sonda: una columna MAX con todo NULL no
# necesita buffer, pero 0 significaría "default del driver" (gigante).
# Piso de la sonda: NUNCA por debajo del default. `max_text_size` es global
# por consulta y aplica también a las columnas de longitud fija; con un piso de
# 64 B, una tabla cuyo LOB estaba en NULL ajustaba el techo a 64 y cualquier
# `varchar` de 135 B truncaba (2 tablas del backup real).
PROBE_FLOOR_BYTES = DEFAULT_MAX_TEXT_SIZE
# Holgura fija por fila al estimar el buffer con techos ajustados (columnas
# acotadas por el driver más cabeceras): 1 MiB sobra para 778 tablas reales.
PROBE_FIXED_SLACK_BYTES = 1024 * 1024


def _buffer_too_small(exc: Exception) -> bool:
    """True si arrow-odbc avisa que un valor no entra en el buffer.

    Dos mensajes distintos según la versión del binding:
    - "a single row does require up to N ... buffer is not large enough";
    - "a value (at least one) is too large to be written into the allocated
      buffer without truncation".
    El segundo apareció en el backup real y no matcheaba: 5 tablas fallaban en
    vez de reintentar con techos más grandes.
    """
    text = str(exc).lower()
    if "single row does require" in text:
        return True
    return "buffer" in text and "truncation" in text


def _tighten_bounds(conn, cfg: Config, db_name: str, ref: TableRef,
                    n_text: int, n_binary: int) -> Config:
    """Achica los techos MAX a los máximos reales (sonda `DATALENGTH`).

    Cuando los techos por defecto (`max_text_size` × columnas MAX) no entran
    en `max_bytes_per_batch`, arrow-odbc se niega a leer la tabla. La foto
    restaurada es estática (nadie escribe), así que el máximo observado ES el
    máximo real: una sola pasada `MAX(DATALENGTH(col))` por columna MAX da
    techos seguros y chicos. Si ni así entra en el presupuesto, lanza
    `_TableTooBig` (la tabla se registra con error y se sigue).
    """
    from dataclasses import replace

    columns = max_column_names(conn, ref, db_name)
    if not columns:
        # Sin columnas MAX no hay buffers variádicos que ajustar: el default es
        # el techo correcto y no hay nada que sondear. Antes se lanzaba
        # _TableTooBig acá, y con la sonda incondicional eso marcaba como error
        # tablas que sí entraban en el presupuesto (mensaje absurdo: "exige
        # ~241 MB" con límite 400 MB).
        return cfg
    cur = conn.cursor()
    cur.execute(probe_max_lengths_sql(db_name, ref, columns))
    row = cur.fetchone()
    vals = list(tuple(row)) if row is not None else []
    if len(vals) != len(columns):
        # Subestimar un techo corrompe el buffer: ante sonda incompleta la
        # tabla queda con error (se sigue) en vez de leer con techos falsos.
        raise RuntimeError(
            f"sonda DATALENGTH incompleta en {ref.display}: {len(vals)} valores "
            f"para {len(columns)} columnas MAX."
        )
    # Los conteos salen de la sonda, no del inventario global: si el inventario
    # falla (se traga la excepción y queda {}) los techos no se ajustaban y las
    # tablas con un LOB de más de 64 KiB fallaban por truncamiento.
    n_text = sum(1 for _, kind in columns if kind != "binary")
    n_binary = sum(1 for _, kind in columns if kind == "binary")
    obs_text = 0
    obs_binary = 0
    for (_, kind), val in zip(columns, vals):
        size = int(val) if val is not None else 0
        if kind == "binary":
            obs_binary = max(obs_binary, size)
        else:
            obs_text = max(obs_text, size)
    # DATALENGTH mide bytes UTF-16 y el tránsito puede viajar en UTF-8 (hasta
    # 1.5x por BMP no ASCII): margen x2. El binario es byte exacto.
    eff = replace(
        cfg,
        max_text_size=max(obs_text * 2, PROBE_FLOOR_BYTES) if n_text else cfg.max_text_size,
        max_binary_size=max(obs_binary, PROBE_FLOOR_BYTES) if n_binary else cfg.max_binary_size,
    )
    row_bytes = (n_text * eff.max_text_size + n_binary * eff.max_binary_size
                 + PROBE_FIXED_SLACK_BYTES)
    eff = replace(eff, max_bytes_per_batch=max(cfg.max_bytes_per_batch, row_bytes))
    need = required_memory_mb(eff, n_text, n_binary)
    if cfg.memory_limit_mb and need > cfg.memory_limit_mb:
        raise _TableTooBig(
            f"tabla {ref.display}: ni con techos ajustados a los datos reales "
            f"(texto≤{eff.max_text_size} B, binario≤{eff.max_binary_size} B) entra "
            f"en el presupuesto: necesita ~{need} MB (límite {cfg.memory_limit_mb} MB)."
        )
    return eff


def _extract_one_table(conn, cfg: Config, db_name: str, db_conn_str: str,
                       ref: TableRef, n_text: int, n_binary: int,
                       max_counts_note: str) -> tuple[int, str | None]:
    """Extrae una tabla con reintento por techos ajustados. Devuelve (filas, nota).

    Camino rápido: techos por defecto (sin sondas). Si arrow-odbc rechaza el
    buffer (una fila no entra), se sondan los tamaños reales y se reintenta
    una vez. `MemoryBudgetExceeded` (RSS medido sobre el límite) se propaga:
    aborta la corrida limpio. `_TableTooBig` (la tabla no entra ni ajustada)
    se propaga como error de esa tabla: el loop la registra y sigue.
    """
    params = cfg
    note = None
    # Se sondean SIEMPRE los tamaños reales (una consulta barata por tabla):
    # el techo por defecto es un piso de seguridad, y dimensionar los buffers
    # con el piso —en vez de con los datos— es lo que colapsa los lotes y
    # dispara la memoria. Sin columnas LOB la sonda devuelve la config intacta.
    # No se usa el inventario global para decidir: si ese inventario fallaba,
    # las tablas con LOB quedaban fuera del ajuste y truncaban.
    params = _tighten_bounds(conn, cfg, db_name, ref, n_text, n_binary)
    if os.environ.get("BAK2PARQUET_DEBUG_BATCHES") == "1":
        print(
            f"  [debug] {ref.display}: text<={params.max_text_size} "
            f"bin<={params.max_binary_size} ajustado={params is not cfg}",
            file=sys.stderr,
        )
    note = None
    if params is not cfg:
        note = (f"techos ajustados a datos reales "
                f"(texto≤{params.max_text_size} B, binario≤{params.max_binary_size} B)")
    source = None
    try:
        source = odbc_batch_source(
            db_conn_str,
            table_query(ref),
            batch_size=params.batch_size,
            max_text_size=params.max_text_size,
            max_binary_size=params.max_binary_size,
            max_bytes_per_batch=params.max_bytes_per_batch,
        )
        try:
            return (
                extract_table(
                    source,
                    dest_for(cfg.out, ref),
                    memory_limit_mb=cfg.memory_limit_mb,
                    context=f"tabla {ref.display}",
                ),
                note,
            )
        except Exception as exc:
            # El reintento no puede depender de que no se hayan ajustado los
            # techos: la sonda puede quedarse corta con un valor atípico, y ese
            # es justo el caso que el reintento cubre.
            if _buffer_too_small(exc):
                print(f"bak2parquet: {ref.display}: buffer chico para una fila: "
                      "sondando tamaños reales y reintentando...", file=sys.stderr)
                params = _tighten_bounds(conn, cfg, db_name, ref, n_text, n_binary)
                note = (f"techos ajustados a datos reales "
                        f"(texto≤{params.max_text_size} B, binario≤{params.max_binary_size} B)")
                try:
                    source.close()
                except Exception:  # noqa: BLE001 — se sigue al reintento.
                    pass
                source = odbc_batch_source(
                    db_conn_str,
                    table_query(ref),
                    batch_size=params.batch_size,
                    max_text_size=params.max_text_size,
                    max_binary_size=params.max_binary_size,
                    max_bytes_per_batch=params.max_bytes_per_batch,
                )
                return (
                    extract_table(
                        source,
                        dest_for(cfg.out, ref),
                        memory_limit_mb=cfg.memory_limit_mb,
                        context=f"tabla {ref.display}",
                    ),
                    note,
                )
            raise
    finally:
        # Higiene determinista (S5.1): el lector arrow-odbc es un statement
        # hijo de su conexión ODBC; se libera el hijo antes que el padre
        # (`BatchSource.close`), con `gc.collect()` en el medio para que el
        # Drop de Rust corra con la conexión aún viva. Sin este orden, cada
        # tabla deja estado del driver huérfano y el RSS crece sin cota.
        if source is not None:
            try:
                source.close()
            except Exception:  # noqa: BLE001 — la higiene no rompe la corrida.
                pass
        # Y devolver al sistema lo que jemalloc retiene: medido en el backup
        # real, el piso crecía ~0,3 MB por tabla (183 -> 312 MB en 400 tablas)
        # mientras pa.total_allocated_bytes() reportaba 0. No es memoria en
        # uso: son páginas ya liberadas que el asignador conserva. Con 778
        # tablas ese colchón agota el presupuesto sin que haya fuga.
        release_allocator_memory()


def release_allocator_memory() -> None:
    """Devuelve al sistema la memoria que el asignador retiene tras liberarla.

    pyarrow usa jemalloc: al liberar conserva las páginas para reutilizarlas y
    el RSS no baja. jemalloc_set_decay_ms(0) desactiva esa retención y
    malloc_trim recorta el heap de glibc. Ambos son best-effort: si no existen
    (otra plataforma u otro asignador) no hay nada que hacer.
    """
    try:
        import pyarrow as pa

        pa.jemalloc_set_decay_ms(0)
    except Exception:  # noqa: BLE001 — sin jemalloc no aplica.
        pass
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001 — sin glibc no hay heap que recortar.
        pass


def run(cfg: Config) -> int:
    """Camino completo: motor → reap → restore → catálogo → extracción → limpieza.

    S5.3 crash-safe: el resumen (`report.json` + texto humano) se escribe en
    TODOS los caminos — éxito, tablas con error y fallas fatales (restore,
    catálogo, presupuesto). Solo un SIGSEGV/OOM externo lo impediría, y para
    eso el reaper del arranque limpia la base huérfana en la próxima corrida.
    """
    db_name = generate_database_name()
    owned = {db_name}
    engine = DockerEngine(cfg) if cfg.engine == "docker" else UrlEngine(cfg)
    report = RunReport(db_name=db_name)
    exit_code = 0

    master = None
    try:
        try:
            engine.start()
        except EngineError as exc:
            raise _Fatal(f"no se pudo arrancar el motor: {exc}") from exc
        print(f"bak2parquet: motor listo, restaurando en [{db_name}]...", file=sys.stderr)

        try:
            master = connect_control(
                engine.connection_string("master"), timeout_sec=cfg.startup_timeout_sec
            )
        except Exception as exc:
            raise _Fatal(f"no se pudo conectar al motor: {exc}") from exc

        # Crash-safe: un SIGSEGV/OOM salta el `finally` y deja la base
        # atrás. Antes de restaurar se barren huérfanas viejas (nunca frescas:
        # podrían ser de una corrida concurrente) y se loguea cada reap.
        for stale in reap_stale_databases(master, max_age_sec=cfg.reap_max_age_sec):
            print(f"bak2parquet: reaped base huérfana [{stale}] de una corrida anterior.",
                  file=sys.stderr)

        try:
            restore_database(master, db_name=db_name, container_bak=engine.bak_path)
        except RestoreError as exc:
            raise _Fatal(f"restauración fallida: {exc}") from exc
        print(f"bak2parquet: base [{db_name}] restaurada.", file=sys.stderr)

        cat_conn = connect_control(engine.connection_string(db_name), timeout_sec=30)
        try:
            tables = list_tables(cat_conn)
            try:
                max_counts = count_all_max_columns(cat_conn)
            except Exception:  # noqa: BLE001 — sin inventario se usa el piso (1, 1).
                max_counts = {}
        except Exception as exc:
            raise _Fatal(f"no se pudo leer el catálogo de [{db_name}]: {exc}") from exc
        finally:
            cat_conn.close()
        print(f"bak2parquet: {len(tables)} tablas base detectadas.", file=sys.stderr)

        db_conn_str = engine.connection_string(db_name)
        for index, ref in enumerate(tables):
            started = time.monotonic()
            tag = f"tabla {index + 1}/{len(tables)} {ref.display}"
            recorded = False
            try:
                check_rss_budget(cfg.memory_limit_mb, context=f"antes de {tag}")
                n_text, n_binary = max_columns_for(ref, max_counts)
                rows, note = _extract_one_table(
                    master, cfg, db_name, db_conn_str, ref, n_text, n_binary,
                    max_counts_note="" if max_counts else " (sin inventario: piso 1, 1)")
                report.results.append(
                    TableResult(schema=ref.schema, table=ref.table, rows=rows,
                                seconds=time.monotonic() - started))
                recorded = True
                extra = f" [{note}]" if note else ""
                print(f"bak2parquet: OK {ref.display}: {rows} filas.{extra}", file=sys.stderr)
                current = rss_mb()
                print(f"bak2parquet: {tag}: rss={current:.0f} MB.", file=sys.stderr)
                check_rss_budget(cfg.memory_limit_mb, context=f"después de {tag}")
            except MemoryBudgetExceeded as exc:
                if not recorded:
                    report.results.append(
                        TableResult(schema=ref.schema, table=ref.table, rows=0,
                                    seconds=time.monotonic() - started, ok=False,
                                    error=str(exc)))
                raise _Fatal(str(exc)) from exc
            except Exception as exc:
                report.results.append(
                    TableResult(schema=ref.schema, table=ref.table, rows=0,
                                seconds=time.monotonic() - started, ok=False, error=str(exc)))
                print(f"bak2parquet: ERROR {ref.display}: {exc}", file=sys.stderr)
            finally:
                gc.collect()
                try:
                    purge_buffers(master)
                except Exception:  # noqa: BLE001 — la higiene no rompe la corrida.
                    pass
        # S5.3: el resumen se escribe siempre — éxito, tablas con error o
        # falla fatal — para que el estado sea verificable sin depender de
        # que el proceso haya sobrevivido hasta el final del loop.
        try:
            report.finish()
            if cfg.report_json:
                report.write_json(cfg.report_json)
            print(render_human(report))
        except Exception as exc:
            print(f"bak2parquet: advertencia: no se pudo escribir el reporte: {exc}",
                  file=sys.stderr)
            exit_code = 1
        if report.fatal_error is not None or report.exit_code != 0:
            exit_code = 1
    except _Fatal as exc:
        report.fatal_error = str(exc)
        print(f"bak2parquet: {exc}", file=sys.stderr)
        # El reporte también se escribe en el camino fatal (S5.3 verificable).
        try:
            report.finish()
            if cfg.report_json:
                report.write_json(cfg.report_json)
            print(render_human(report))
        except Exception as exc2:  # noqa: BLE001 — el teardown sigue igual.
            print(f"bak2parquet: advertencia: no se pudo escribir el reporte: {exc2}",
                  file=sys.stderr)
        exit_code = 1
    finally:
        # S5.2: la base temporal se elimina siempre, aun ante fallo irrecuperable.
        if master is not None:
            if not cfg.keep_db:
                try:
                    drop_database(master, db_name, owned=owned)
                except RefusedDropError as exc:  # No debería pasar: el nombre es generado.
                    print(f"bak2parquet: advertencia: {exc}", file=sys.stderr)
                except Exception as exc:
                    print(f"bak2parquet: advertencia: no se pudo borrar [{db_name}]: {exc}",
                          file=sys.stderr)
            try:
                master.close()
            except Exception:  # noqa: BLE001 — el teardown no rompe el exit code.
                pass
        try:
            engine.stop()  # En modo url es no-op: nunca detiene el motor.
        except Exception:  # noqa: BLE001 — idem.
            pass
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Entrada del paquete: parsea, valida, acota memoria y corre."""
    args = build_parser().parse_args(argv)
    try:
        cfg = config_from_parsed(args)
    except ConfigError as exc:
        print(f"bak2parquet: configuración inválida: {exc}", file=sys.stderr)
        return 2
    return run(cfg)
