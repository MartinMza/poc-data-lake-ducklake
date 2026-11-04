"""Resumen final de la corrida (T7, S5.3).

Detectadas, exportadas, filas, tiempo por tabla y tablas con error.
`--report-json` lo deja en disco; exit 0 solo si todo se exportó.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field


@dataclass
class TableResult:
    """Resultado de una tabla: filas, segundos y error si lo hubo."""

    schema: str
    table: str
    rows: int = 0
    seconds: float = 0.0
    ok: bool = True
    error: str | None = None

    @property
    def display(self) -> str:
        """Nombre legible `schema.tabla`."""
        return f"{self.schema}.{self.table}"


@dataclass
class RunReport:
    """Resumen de la corrida (S5.3)."""

    db_name: str
    results: list[TableResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    fatal_error: str | None = None

    def finish(self) -> RunReport:
        """Cierra el cronómetro total."""
        self.ended_at = time.time()
        return self

    @property
    def n_detected(self) -> int:
        """Tablas detectadas en el catálogo."""
        return len(self.results)

    @property
    def n_ok(self) -> int:
        """Tablas exportadas con éxito."""
        return sum(1 for r in self.results if r.ok)

    @property
    def n_failed(self) -> int:
        """Tablas que arrojaron error."""
        return sum(1 for r in self.results if not r.ok)

    @property
    def total_rows(self) -> int:
        """Filas procesadas en total."""
        return sum(r.rows for r in self.results)

    @property
    def exit_code(self) -> int:
        """0 solo si todas las tablas se exportaron (contrato de salida)."""
        return 0 if self.n_failed == 0 else 1

    def to_dict(self) -> dict:
        """Serialización para `--report-json`."""
        return {
            "db_name": self.db_name,
            "detected": self.n_detected,
            "exported_ok": self.n_ok,
            "failed": self.n_failed,
            "total_rows": self.total_rows,
            "elapsed_sec": round(self.ended_at - self.started_at, 3) if self.ended_at else None,
            "fatal_error": self.fatal_error,
            "tables": [asdict(r) for r in self.results],
        }

    def write_json(self, path: str) -> str:
        """Escribe el resumen en JSON. Devuelve la ruta."""
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return path


def render_human(report: RunReport) -> str:
    """Resumen legible: conteos, filas, tiempo por tabla y errores (S5.3)."""
    lines = [
        f"base temporal: {report.db_name}",
        f"tablas detectadas: {report.n_detected}",
        f"tablas exportadas: {report.n_ok}",
        f"filas procesadas: {report.total_rows}",
    ]
    for r in report.results:
        if r.ok:
            lines.append(f"  OK   {r.display}: {r.rows} filas en {r.seconds:.1f}s")
        else:
            lines.append(f"  ERROR {r.display}: {r.error or 'falló'} ({r.seconds:.1f}s)")
    if report.fatal_error:
        lines.append(f"error fatal: {report.fatal_error}")
    return "\n".join(lines)
