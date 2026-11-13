"""Pruebas del panel del sensor de presencia.

`SensorActivity` es Python puro y se prueba sin Qt. El panel se construye en
modo `offscreen` (ver `conftest.py`) y se comprueba su estado, no sus pixeles:
un test de imagen seria fragil y no diria nada del comportamiento.
"""

from __future__ import annotations

import time

from alarm.ui.sensor_panel import SAMPLE_S, WINDOW_S, SensorActivity

# ---------------------------------------------------------------------------
# SensorActivity: muestreo, ciclo de trabajo y franja
# ---------------------------------------------------------------------------


def test_la_rejilla_avanza_a_medio_segundo() -> None:
    activity = SensorActivity()
    start = 1_000.0
    activity.tick(start, False)
    activity.tick(start + SAMPLE_S, False)
    activity.tick(start + 2 * SAMPLE_S, True)

    assert len(activity.samples()) == 3
    assert activity.samples() == (False, False, True)


def test_la_ventana_no_crece_mas_alla_de_sesenta_segundos() -> None:
    activity = SensorActivity()
    start = 1_000.0
    for index in range(int(WINDOW_S / SAMPLE_S) + 40):
        activity.tick(start + index * SAMPLE_S, index % 2 == 0)

    capacidad = int(WINDOW_S / SAMPLE_S)
    assert len(activity.samples()) <= capacidad


def test_el_ciclo_de_trabajo_cuenta_los_ultimos_segundos() -> None:
    activity = SensorActivity()
    start = 1_000.0
    # 40 muestras (20 s); movimiento en las 10 ultimas, que son 5 s.
    for index in range(40):
        activity.tick(start + index * SAMPLE_S, index >= 30)

    duty = activity.duty(start + 39 * SAMPLE_S, window_s=10.0)
    assert 45.0 <= duty <= 55.0, duty


def test_el_ciclo_de_trabajo_sin_muestras_es_cero() -> None:
    assert SensorActivity().duty(1_000.0) == 0.0


def test_la_ultima_deteccion_se_mide_en_segundos() -> None:
    activity = SensorActivity()
    start = 500.0
    activity.tick(start, False)
    assert activity.last_motion_age(start) is None

    activity.tick(start + 5.0, True)
    assert activity.last_motion_age(start + 8.0) == 3.0


def test_record_motion_apunta_el_pulso_de_inmediato() -> None:
    activity = SensorActivity()
    activity.tick(1_000.0, False)
    activity.record_motion(1_002.0)

    assert activity.pulses == 1
    assert activity.samples()[-1] is True
    assert activity.last_motion_age(1_002.0) == 0.0


def test_un_salto_grande_de_reloj_no_rellena_la_franja() -> None:
    """Suspender el equipo seis horas no debe llenar la ventana de muestras."""
    activity = SensorActivity()
    activity.tick(1_000.0, False)
    activity.tick(1_000.0 + 6 * 3600, True)

    assert len(activity.samples()) <= int(WINDOW_S / SAMPLE_S) + 2


def test_reset_borra_todo() -> None:
    activity = SensorActivity()
    activity.tick(1_000.0, True)
    activity.record_motion(1_001.0)
    activity.reset()

    assert activity.samples() == ()
    assert activity.pulses == 0
    assert activity.last_motion_age(2_000.0) is None


# ---------------------------------------------------------------------------
# SensorPanel: estado y pintura sin ventana
# ---------------------------------------------------------------------------


def _panel(qapp: object):
    from alarm.ui.sensor_panel import SensorPanel

    panel = SensorPanel()
    panel.resize(440, 196)
    return panel


def test_el_panel_arranca_sin_enlace(qapp) -> None:
    panel = _panel(qapp)
    try:
        assert panel.level is False
        assert panel.energy == 0.0
    finally:
        panel.deleteLater()


def test_el_nivel_enciende_las_barras(qapp) -> None:
    panel = _panel(qapp)
    try:
        panel.set_connected(True)
        now = time.time()
        panel.set_level(True, now=now - 0.6)
        panel.set_level(True, now=now)
        assert panel.level is True
        assert panel.energy > 0.0, "el movimiento debe levantar las barras"

        panel.set_level(False, now=now + 5.0)
        assert panel.level is False
        assert panel.energy < 1.0, "al cesar el movimiento las barras bajan"
    finally:
        panel.deleteLater()


def test_desconectar_apaga_el_panel(qapp) -> None:
    panel = _panel(qapp)
    try:
        panel.set_connected(True)
        panel.set_level(True)
        panel.set_connected(False)
        assert panel.level is False
        assert panel.energy == 0.0
    finally:
        panel.deleteLater()


def test_simular_rellena_la_franja_para_las_capturas(qapp) -> None:
    panel = _panel(qapp)
    try:
        panel.simulate()
        muestras = panel.activity.samples()
        assert len(muestras) >= int(WINDOW_S / SAMPLE_S) - 2
        assert any(muestras), "la franja de ejemplo debe tener movimiento"
        assert panel.energy > 0.5
    finally:
        panel.deleteLater()


def test_el_panel_se_puede_pintar_en_todos_los_estados(qapp) -> None:
    """Pintar en `offscreen` no debe lanzar: cubre las tres ramas del dibujo."""
    panel = _panel(qapp)
    try:
        for connected, level in ((False, False), (True, False), (True, True)):
            panel.set_connected(connected)
            panel.set_level(level)
            panel.tick()
            image = panel.grab()
            assert not image.isNull()
            assert image.width() == panel.width()
    finally:
        panel.deleteLater()


def test_el_temporizador_solo_corre_si_hay_algo_que_animar(qapp) -> None:
    panel = _panel(qapp)
    try:
        assert panel._timer.isActive() is False
        panel.set_connected(True)
        assert panel._timer.isActive() is True
        panel.set_connected(False)
        assert panel._timer.isActive() is False
    finally:
        panel.deleteLater()
