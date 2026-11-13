"""Pruebas de la maquina de estados minima."""

from __future__ import annotations

import pytest

from alarm.core.state import (
    ACTION_ARMED,
    ACTION_ARMING,
    ACTION_DISARMED,
    ACTION_PENDING,
    ACTION_REARM_EXHAUSTED,
    ACTION_REARMED,
    ACTION_REARMING,
    ACTION_SILENCED,
    ACTION_TRIGGERED,
    AlarmController,
    AlarmState,
)
from alarm.protocol.messages import MotionEvent, MotionKind


def _start(seq: int = 1) -> MotionEvent:
    return MotionEvent(seq=seq, kind=MotionKind.START, zone=1, ms=1000)


def _end(seq: int = 2) -> MotionEvent:
    return MotionEvent(seq=seq, kind=MotionKind.END, zone=1, ms=3700, dur_ms=2700)


def test_arranca_desarmada() -> None:
    controller = AlarmController()
    assert controller.state is AlarmState.DISARMED
    assert controller.armed is False


def test_arranca_armada_si_se_pide() -> None:
    assert AlarmController(armed_on_start=True).state is AlarmState.ARMED


def test_armar_y_desarmar() -> None:
    controller = AlarmController()

    armed = controller.arm()
    assert armed is not None
    assert armed.action == ACTION_ARMED
    assert armed.state_changed
    assert controller.state is AlarmState.ARMED

    assert controller.arm() is None

    disarmed = controller.disarm()
    assert disarmed is not None
    assert disarmed.action == ACTION_DISARMED
    assert controller.state is AlarmState.DISARMED
    assert controller.disarm() is None


def test_sin_armar_no_dispara() -> None:
    controller = AlarmController()
    assert controller.feed(_start()) is None
    assert controller.state is AlarmState.DISARMED


def test_motion_start_armada_dispara() -> None:
    controller = AlarmController(armed_on_start=True)
    transition = controller.feed(_start())
    assert transition is not None
    assert transition.action == ACTION_TRIGGERED
    assert controller.state is AlarmState.TRIGGERED


def test_motion_end_no_dispara() -> None:
    controller = AlarmController(armed_on_start=True)
    assert controller.feed(_end()) is None
    assert controller.state is AlarmState.ARMED


def test_disparada_no_se_repite() -> None:
    controller = AlarmController(armed_on_start=True)
    assert controller.feed(_start()) is not None
    assert controller.feed(_start(seq=5)) is None


def test_silenciar_no_desarma() -> None:
    controller = AlarmController(armed_on_start=True)
    controller.feed(_start())
    transition = controller.silence()
    assert transition is not None
    assert transition.action == ACTION_SILENCED
    assert controller.state is AlarmState.TRIGGERED
    assert controller.silenced is True
    assert controller.armed is True
    assert controller.silence() is None


def test_enfriamiento_por_causa() -> None:
    now = [100.0]
    controller = AlarmController(cooldown_s=60, max_per_hour=10, clock=lambda: now[0])

    assert controller.allow("triggered") is True
    now[0] += 30
    assert controller.allow("triggered") is False
    # Otra causa no comparte enfriamiento.
    assert controller.allow("armed") is True
    now[0] += 31
    assert controller.allow("triggered") is True


def test_tope_por_hora() -> None:
    now = [0.0]
    controller = AlarmController(cooldown_s=0, max_per_hour=2, clock=lambda: now[0])

    assert controller.allow("triggered") is True
    now[0] += 10
    assert controller.allow("triggered") is True
    now[0] += 10
    assert controller.allow("triggered") is False
    # Al pasar la hora, la ventana se vacia.
    now[0] += 3600
    assert controller.allow("triggered") is True


def test_set_limits_actualiza_los_limites() -> None:
    now = [0.0]
    controller = AlarmController(cooldown_s=0, max_per_hour=1, clock=lambda: now[0])
    assert controller.allow("x") is True
    assert controller.allow("y") is False
    controller.set_limits(cooldown_s=0, max_per_hour=5)
    now[0] += 10
    assert controller.allow("y") is True


def test_reset_limits() -> None:
    controller = AlarmController(cooldown_s=1000, max_per_hour=1)
    assert controller.allow("triggered") is True
    assert controller.allow("triggered") is False
    controller.reset_limits()
    assert controller.allow("triggered") is True


# ---------------------------------------------------------------------------
# Retardos de entrada y salida
# ---------------------------------------------------------------------------


def test_el_retardo_de_salida_retrasa_el_armado() -> None:
    now = [0.0]
    controller = AlarmController(exit_delay_s=20, clock=lambda: now[0])

    transition = controller.arm()
    assert transition is not None
    assert transition.action == ACTION_ARMING
    assert controller.state is AlarmState.ARMING
    assert controller.delay_kind == "exit"
    assert controller.armed is False, "durante la salida el PIR no debe vigilar"
    assert controller.remaining_s() == 20
    assert controller.delay_total_s == 20, "el reloj necesita saber el plazo completo"

    now[0] = 19.9
    assert controller.tick() is None
    assert controller.state is AlarmState.ARMING

    now[0] = 20.0
    transition = controller.tick()
    assert transition is not None
    assert transition.action == ACTION_ARMED
    assert controller.state is AlarmState.ARMED
    assert controller.armed is True
    assert controller.remaining_s() is None
    assert controller.delay_total_s == 0, "sin retardo no queda plazo que dibujar"


def test_cambiar_los_retardos_no_descuadra_el_plazo_en_curso() -> None:
    """El reloj grande mide contra el plazo con el que se abrio la cuenta."""
    now = [0.0]
    controller = AlarmController(exit_delay_s=20, clock=lambda: now[0])
    controller.arm()

    controller.set_delays(exit_delay_s=5, entry_delay_s=5)
    assert controller.delay_total_s == 20, "recortarlo a mitad armaria de golpe"
    assert controller.remaining_s() == 20

    controller.disarm()
    assert controller.delay_total_s == 0


def test_el_movimiento_al_salir_no_dispara() -> None:
    controller = AlarmController(exit_delay_s=20)
    controller.arm()
    assert controller.feed(_start()) is None
    assert controller.state is AlarmState.ARMING


def test_desarmar_cancela_el_retardo_de_salida() -> None:
    now = [0.0]
    controller = AlarmController(exit_delay_s=20, clock=lambda: now[0])
    controller.arm()

    transition = controller.disarm()
    assert transition is not None
    assert transition.action == ACTION_DISARMED
    assert controller.state is AlarmState.DISARMED
    assert controller.remaining_s() is None

    now[0] = 100.0
    assert controller.tick() is None
    assert controller.state is AlarmState.DISARMED


def test_el_retardo_de_entrada_da_tiempo_a_desarmar() -> None:
    now = [0.0]
    controller = AlarmController(armed_on_start=True, entry_delay_s=30, clock=lambda: now[0])

    transition = controller.feed(_start())
    assert transition is not None
    assert transition.action == ACTION_PENDING
    assert controller.state is AlarmState.PENDING
    assert controller.delay_kind == "entry"
    assert controller.armed is True, "el dispositivo sigue armado: sigue vigilando"
    assert controller.remaining_s() == 30

    now[0] = 12.0
    assert controller.feed(_start(seq=2)) is None, "otra deteccion no reinicia el plazo"
    assert controller.remaining_s() == 18

    transition = controller.disarm()
    assert transition is not None
    assert transition.action == ACTION_DISARMED
    assert controller.state is AlarmState.DISARMED
    assert controller.tick() is None


def test_el_retardo_de_entrada_dispara_al_vencer() -> None:
    now = [0.0]
    controller = AlarmController(armed_on_start=True, entry_delay_s=30, clock=lambda: now[0])
    controller.feed(_start())

    now[0] = 29.9
    assert controller.tick() is None
    now[0] = 30.0
    transition = controller.tick()
    assert transition is not None
    assert transition.action == ACTION_TRIGGERED
    assert transition.previous is AlarmState.PENDING
    assert controller.state is AlarmState.TRIGGERED
    assert controller.remaining_s() is None


def test_silenciar_durante_el_retardo_de_entrada_no_hace_nada() -> None:
    controller = AlarmController(armed_on_start=True, entry_delay_s=30)
    controller.feed(_start())
    assert controller.silence() is None
    assert controller.state is AlarmState.PENDING


def test_retardos_a_cero_son_instantaneos() -> None:
    controller = AlarmController(exit_delay_s=0, entry_delay_s=0)
    armed = controller.arm()
    assert armed is not None
    assert armed.action == ACTION_ARMED
    assert controller.state is AlarmState.ARMED

    triggered = controller.feed(_start())
    assert triggered is not None
    assert triggered.action == ACTION_TRIGGERED


def test_set_delays_no_recorta_el_retardo_en_curso() -> None:
    now = [0.0]
    controller = AlarmController(exit_delay_s=20, clock=lambda: now[0])
    controller.arm()

    controller.set_delays(exit_delay_s=1, entry_delay_s=5)
    assert controller.remaining_s() == 20, "el plazo actual no se toca a mitad"

    now[0] = 20.0
    assert controller.tick() is not None
    # El siguiente armado ya usa el valor nuevo.
    assert controller.disarm() is not None
    controller.arm()
    assert controller.remaining_s() == 1


def test_arrancar_armada_no_aplica_el_retardo_de_salida() -> None:
    controller = AlarmController(armed_on_start=True, exit_delay_s=20)
    assert controller.state is AlarmState.ARMED
    assert controller.remaining_s() is None
    assert controller.tick() is None


# ---------------------------------------------------------------------------
# Rearme automatico tras un disparo
# ---------------------------------------------------------------------------


def _con_rearme(now: list[float], **kwargs: object) -> AlarmController:
    """Controlador armado con rearme automatico y reloj falso."""
    options: dict[str, object] = {
        "armed_on_start": True,
        "auto_rearm": True,
        "rearm_delay_s": 300,
        "max_rearms": 2,
        "siren_s": 180,
    }
    options.update(kwargs)
    return AlarmController(clock=lambda: now[0], **options)  # type: ignore[arg-type]


def test_la_sirena_abre_la_espera_del_rearme() -> None:
    now = [0.0]
    controller = _con_rearme(now)

    assert controller.feed(_start()) is not None
    assert controller.state is AlarmState.TRIGGERED
    assert controller.siren_remaining_s() == 180

    now[0] = 179.0
    assert controller.tick() is None
    assert controller.state is AlarmState.TRIGGERED, "mientras suena no se rearma"

    now[0] = 180.0
    transition = controller.tick()
    assert transition is not None
    assert transition.action == ACTION_REARMING
    assert controller.state is AlarmState.REARMING
    assert controller.delay_kind == "rearm", "el hero reutiliza el reloj grande"
    assert controller.remaining_s() == 300
    assert controller.delay_total_s == 300
    assert controller.armed is True, "el dispositivo sigue armado durante la espera"
    assert controller.rearm_count == 0


def test_el_rearme_devuelve_la_alarma_a_vigilar() -> None:
    now = [0.0]
    controller = _con_rearme(now)
    controller.feed(_start())
    now[0] = 180.0
    controller.tick()

    now[0] = 479.0
    assert controller.tick() is None
    now[0] = 480.0
    transition = controller.tick()
    assert transition is not None
    assert transition.action == ACTION_REARMED
    assert controller.state is AlarmState.ARMED
    assert controller.rearm_count == 1
    assert controller.remaining_s() is None
    assert controller.siren_remaining_s() is None


def test_el_rearme_no_pasa_por_el_retardo_de_salida() -> None:
    """Quien ha disparado ya esta dentro: no hay que darle tiempo a salir."""
    now = [0.0]
    controller = _con_rearme(now, exit_delay_s=45)
    controller.feed(_start())
    now[0] = 180.0
    controller.tick()
    now[0] = 480.0

    transition = controller.tick()
    assert transition is not None
    assert transition.action == ACTION_REARMED
    assert controller.state is AlarmState.ARMED


def test_una_espera_cero_rearma_al_instante() -> None:
    now = [0.0]
    controller = _con_rearme(now, rearm_delay_s=0)
    controller.feed(_start())
    now[0] = 180.0

    transition = controller.tick()
    assert transition is not None
    assert transition.action == ACTION_REARMED
    assert controller.state is AlarmState.ARMED


def test_silenciar_adelanta_la_espera_del_rearme() -> None:
    """La espera cuenta desde que calla la sirena, no desde el disparo."""
    now = [0.0]
    controller = _con_rearme(now)
    controller.feed(_start())

    now[0] = 20.0
    assert controller.silence() is not None
    now[0] = 20.0
    transition = controller.tick()
    assert transition is not None
    assert transition.action == ACTION_REARMING
    assert controller.state is AlarmState.REARMING


def test_el_numero_maximo_de_rearmes_corta_el_bucle() -> None:
    now = [0.0]
    controller = _con_rearme(now, max_rearms=1, rearm_delay_s=10, siren_s=5)

    controller.feed(_start())
    now[0] = 5.0
    assert controller.tick() is not None
    now[0] = 15.0
    transition = controller.tick()
    assert transition is not None
    assert transition.action == ACTION_REARMED
    assert controller.rearm_count == 1

    # Segundo disparo: ya no quedan rearmes.
    assert controller.feed(_start(seq=9)) is not None
    now[0] = 20.0
    transition = controller.tick()
    assert transition is not None
    assert transition.action == ACTION_REARM_EXHAUSTED
    assert controller.state is AlarmState.TRIGGERED
    assert controller.rearm_exhausted is True

    # El aviso se da una sola vez: no puede repetirse en cada tic.
    now[0] = 900.0
    assert controller.tick() is None
    assert controller.state is AlarmState.TRIGGERED


def test_sin_rearme_automatico_el_disparo_no_cambia() -> None:
    now = [0.0]
    controller = AlarmController(
        armed_on_start=True, auto_rearm=False, clock=lambda: now[0], siren_s=5
    )
    controller.feed(_start())

    now[0] = 1000.0
    assert controller.tick() is None
    assert controller.state is AlarmState.TRIGGERED


def test_desarmar_reinicia_el_contador_de_rearmes() -> None:
    now = [0.0]
    controller = _con_rearme(now, rearm_delay_s=0, siren_s=5)
    controller.feed(_start())
    now[0] = 5.0
    assert controller.tick() is not None
    assert controller.rearm_count == 1

    assert controller.disarm() is not None
    assert controller.rearm_count == 0

    assert controller.arm() is not None
    assert controller.rearm_count == 0


def test_desarmar_cancela_la_espera_del_rearme() -> None:
    now = [0.0]
    controller = _con_rearme(now)
    controller.feed(_start())
    now[0] = 180.0
    controller.tick()
    assert controller.state is AlarmState.REARMING

    assert controller.disarm() is not None
    assert controller.state is AlarmState.DISARMED
    now[0] = 10000.0
    assert controller.tick() is None
    assert controller.state is AlarmState.DISARMED


def test_reiniciar_la_sirena_renueva_su_plazo() -> None:
    """La placa pierde el patron al reconectar: el plazo debe empezar de nuevo."""
    now = [0.0]
    controller = _con_rearme(now)
    controller.feed(_start())

    now[0] = 170.0
    controller.restart_siren()
    assert controller.siren_remaining_s() == 180

    now[0] = 200.0
    assert controller.tick() is None, "todavia esta sonando la sirena retomada"


def test_set_rearm_no_toca_la_espera_en_curso() -> None:
    now = [0.0]
    controller = _con_rearme(now)
    controller.feed(_start())
    now[0] = 180.0
    controller.tick()

    controller.set_rearm(auto_rearm=True, rearm_delay_s=1, max_rearms=9)
    assert controller.remaining_s() == 300, "el plazo abierto no se recorta a mitad"
    assert controller.rearm_max == 9


# ---------------------------------------------------------------------------
# Snapshot: recuperar la alarma tal y como estaba
# ---------------------------------------------------------------------------

_CONFIG: dict[str, object] = {
    "exit_delay_s": 0,
    "entry_delay_s": 30,
    "auto_rearm": True,
    "rearm_delay_s": 300,
    "max_rearms": 2,
    "siren_s": 180,
}


def _opciones(**kwargs: object) -> dict[str, object]:
    options = dict(_CONFIG)
    options.update(kwargs)
    return options


def _controlador(now: list[float], **kwargs: object) -> AlarmController:
    return AlarmController(clock=lambda: now[0], **_opciones(**kwargs))  # type: ignore[arg-type]


def _en_estado(state: AlarmState, now: list[float]) -> AlarmController:
    """Controlador colocado en `state` con un reloj falso."""
    if state is AlarmState.ARMING:
        controller = _controlador(now, exit_delay_s=45)
        controller.arm()
        now[0] += 10
    elif state is AlarmState.REARMING:
        controller = _controlador(now, entry_delay_s=0)
        controller.arm()
        controller.feed(_start())
        now[0] += 180
        controller.tick()
        now[0] += 20
    elif state is AlarmState.PENDING:
        controller = _controlador(now)
        controller.arm()
        controller.feed(_start())
        now[0] += 5
    elif state is AlarmState.TRIGGERED:
        controller = _controlador(now, entry_delay_s=0)
        controller.arm()
        controller.feed(_start())
        now[0] += 10
    elif state is AlarmState.ARMED:
        controller = _controlador(now)
        controller.arm()
    else:
        controller = _controlador(now)
    assert controller.state is state
    return controller


@pytest.mark.parametrize("state", list(AlarmState))
def test_el_snapshot_va_y_vuelve_para_cada_estado(state: AlarmState) -> None:
    now = [100.0]
    original = _en_estado(state, now)
    snapshot = original.to_snapshot()

    restaurado = AlarmController.from_snapshot(
        snapshot,
        clock=lambda: now[0],
        **_opciones(),  # type: ignore[arg-type]
    )

    assert restaurado.state is state
    assert restaurado.armed is original.armed
    assert restaurado.silenced == original.silenced
    assert restaurado.rearm_count == original.rearm_count
    if original.remaining_s() is None:
        assert restaurado.remaining_s() is None
    else:
        assert restaurado.remaining_s() == pytest.approx(original.remaining_s(), abs=0.01)
        assert restaurado.delay_total_s == pytest.approx(original.delay_total_s, abs=0.01)
    if original.siren_remaining_s() is None:
        assert restaurado.siren_remaining_s() is None
    else:
        assert restaurado.siren_remaining_s() == pytest.approx(
            original.siren_remaining_s(), abs=0.01
        )


def test_un_retardo_de_entrada_vencido_dispara_al_recuperar() -> None:
    """Cerrar el PC en PENDING y volver tarde no puede reabrir el plazo."""
    now = [0.0]
    controller = _controlador(now)
    controller.arm()
    controller.feed(_start())
    now[0] += 5
    snapshot = controller.to_snapshot()

    restaurado = AlarmController.from_snapshot(
        snapshot,
        elapsed_s=600,
        clock=lambda: now[0],
        **_opciones(),  # type: ignore[arg-type]
    )
    assert restaurado.state is AlarmState.PENDING
    transition = restaurado.tick()
    assert transition is not None
    assert transition.action == ACTION_TRIGGERED
    assert restaurado.state is AlarmState.TRIGGERED


def test_un_retardo_de_salida_vencido_arma_al_recuperar() -> None:
    now = [0.0]
    controller = _controlador(now, exit_delay_s=45)
    controller.arm()
    now[0] += 10
    snapshot = controller.to_snapshot()

    restaurado = AlarmController.from_snapshot(
        snapshot,
        elapsed_s=600,
        clock=lambda: now[0],
        **_opciones(exit_delay_s=45),  # type: ignore[arg-type]
    )
    transition = restaurado.tick()
    assert transition is not None
    assert transition.action == ACTION_ARMED
    assert restaurado.state is AlarmState.ARMED


def test_una_espera_de_rearme_vencida_vuelve_a_vigilar() -> None:
    now = [0.0]
    controller = _controlador(now, entry_delay_s=0)
    controller.arm()
    controller.feed(_start())
    now[0] += 180
    controller.tick()
    now[0] += 20
    assert controller.state is AlarmState.REARMING
    snapshot = controller.to_snapshot()

    restaurado = AlarmController.from_snapshot(
        snapshot,
        elapsed_s=600,
        clock=lambda: now[0],
        **_opciones(),  # type: ignore[arg-type]
    )
    transition = restaurado.tick()
    assert transition is not None
    assert transition.action == ACTION_REARMED
    assert restaurado.state is AlarmState.ARMED


def test_recuperar_un_disparo_conserva_el_tiempo_de_sirena() -> None:
    now = [0.0]
    controller = _controlador(now, entry_delay_s=0)
    controller.arm()
    controller.feed(_start())
    now[0] += 30
    snapshot = controller.to_snapshot()

    restaurado = AlarmController.from_snapshot(
        snapshot,
        elapsed_s=0,
        clock=lambda: now[0],
        **_opciones(),  # type: ignore[arg-type]
    )
    assert restaurado.state is AlarmState.TRIGGERED
    assert restaurado.siren_remaining_s() == pytest.approx(150, abs=0.01)
    assert restaurado.tick() is None, "aun le queda sirena"


def test_recuperar_un_disparo_con_la_sirena_vencida_rearma() -> None:
    now = [0.0]
    controller = _controlador(now, entry_delay_s=0)
    controller.arm()
    controller.feed(_start())
    now[0] += 10
    snapshot = controller.to_snapshot()

    restaurado = AlarmController.from_snapshot(
        snapshot,
        elapsed_s=200,
        clock=lambda: now[0],
        **_opciones(),  # type: ignore[arg-type]
    )
    transition = restaurado.tick()
    assert transition is not None
    assert transition.action == ACTION_REARMING
    assert restaurado.state is AlarmState.REARMING


def test_el_snapshot_conserva_silenciada_y_rearmes() -> None:
    now = [0.0]
    controller = _controlador(now, entry_delay_s=0)
    controller.arm()
    controller.feed(_start())
    controller.silence()
    now[0] += 10

    snapshot = controller.to_snapshot()
    restaurado = AlarmController.from_snapshot(
        snapshot,
        clock=lambda: now[0],
        **_opciones(),  # type: ignore[arg-type]
    )

    assert restaurado.state is AlarmState.TRIGGERED
    assert restaurado.silenced is True
    assert restaurado.rearm_count == controller.rearm_count


def test_un_estado_guardado_ilegible_arranca_desarmado() -> None:
    restaurado = AlarmController.from_snapshot(
        {"state": "inventado"},
        **_opciones(),  # type: ignore[arg-type]
    )
    assert restaurado.state is AlarmState.DISARMED
    assert restaurado.armed is False
