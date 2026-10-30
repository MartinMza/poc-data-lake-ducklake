"""Rate-limiting de login (anti fuerza bruta de contraseñas).

Reloj inyectable para probar ventanas y lockout sin dormir.
"""

from __future__ import annotations

from dulse.adapters.outbound.memory_throttle import LoginThrottle


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _throttle(clock: _Clock) -> LoginThrottle:
    return LoginThrottle(max_attempts=3, window_seconds=60, lockout_seconds=120, clock=clock)


def test_por_debajo_del_umbral_no_bloquea():
    clock = _Clock()
    t = _throttle(clock)
    t.register_failure("ana")
    t.register_failure("ana")
    assert t.locked_for("ana") == 0.0


def test_al_alcanzar_el_umbral_bloquea():
    clock = _Clock()
    t = _throttle(clock)
    for _ in range(3):
        t.register_failure("ana")
    assert t.locked_for("ana") == 120.0


def test_exito_limpia_los_intentos():
    clock = _Clock()
    t = _throttle(clock)
    t.register_failure("ana")
    t.register_failure("ana")
    t.register_success("ana")
    t.register_failure("ana")
    assert t.locked_for("ana") == 0.0


def test_el_lockout_vence():
    clock = _Clock()
    t = _throttle(clock)
    for _ in range(3):
        t.register_failure("ana")
    assert t.locked_for("ana") == 120.0
    clock.advance(120.0)
    assert t.locked_for("ana") == 0.0


def test_intentos_fuera_de_la_ventana_no_cuentan():
    clock = _Clock()
    t = _throttle(clock)
    t.register_failure("ana")
    t.register_failure("ana")
    clock.advance(61.0)  # los dos primeros salen de la ventana
    t.register_failure("ana")
    assert t.locked_for("ana") == 0.0


def test_claves_independientes():
    clock = _Clock()
    t = _throttle(clock)
    for _ in range(3):
        t.register_failure("ana")
    assert t.locked_for("ana") == 120.0
    assert t.locked_for("root") == 0.0


def test_clave_normaliza_mayusculas_y_espacios():
    clock = _Clock()
    t = _throttle(clock)
    for _ in range(3):
        t.register_failure("  Ana ")
    assert t.locked_for("ana") == 120.0
