"""Pruebas del modo de ajuste del sensor."""

from __future__ import annotations

from alarm.device.calibrator import Calibrator, Level, SessionStats, evaluate, recommendation
from alarm.protocol.messages import CalibDone, CalibEdge, CalibWindow


def _feed_edges(cal: Calibrator, spans: list[tuple[int, int]]) -> None:
    for start, end in spans:
        cal.feed(CalibEdge(ms=start, level=True, dt_ms=0))
        cal.feed(CalibEdge(ms=end, level=False, dt_ms=end - start))


def _done(cal: Calibrator, edges: int, duration_ms: int, high_ms: int, bounces: int = 0) -> None:
    cal.feed(
        CalibDone(
            elapsed_ms=duration_ms,
            edges=edges,
            high_ms=high_ms,
            duty_pct=round(high_ms * 100.0 / duration_ms, 1),
            min_gap_ms=0,
            max_gap_ms=0,
            gaps=max(0, edges - 1),
            bounces=bounces,
        )
    )


def test_calibrador_parea_flancos() -> None:
    cal = Calibrator("motion")
    _feed_edges(cal, [(1000, 3700), (8000, 10700), (18000, 20700)])
    stats = cal.stats()
    assert stats.pulses == 3
    assert stats.holds_ms == [2700, 2700, 2700]
    assert stats.hold_avg_ms == 2700
    assert stats.hold_is_uniform is True


def test_calibrador_usa_los_totales_del_dispositivo() -> None:
    cal = Calibrator("quiet")
    _feed_edges(cal, [(1000, 3700)])
    _done(cal, edges=9, duration_ms=30000, high_ms=8100, bounces=2)
    stats = cal.stats()
    assert cal.finished is True
    assert stats.edges == 9
    assert stats.duration_s == 30.0
    assert stats.duty_pct == 27.0
    assert stats.bounces == 2


def test_calibrador_pulso_abierto_al_cerrar() -> None:
    cal = Calibrator("motion")
    cal.feed(CalibEdge(ms=1000, level=True, dt_ms=0))
    stats = cal.stats()
    assert stats.pulses == 0
    assert stats.edges == 1


def test_ventana_actualiza_rebotes() -> None:
    cal = Calibrator("quiet")
    cal.feed(
        CalibWindow(
            ms=5000,
            elapsed_ms=5000,
            edges=0,
            high_ms=0,
            duty_pct=0.0,
            min_gap_ms=0,
            max_gap_ms=0,
            bounces=4,
        )
    )
    assert cal.stats().bounces == 4


# -- Diagnostico ------------------------------------------------------------


def _stats(**kw) -> SessionStats:
    base = dict(
        phase="x",
        duration_s=30.0,
        edges=0,
        pulses=0,
        holds_ms=[],
        high_ms=0,
        duty_pct=0.0,
        bounces=0,
        min_gap_ms=0,
        max_gap_ms=0,
    )
    base.update(kw)
    return SessionStats(**base)  # type: ignore[arg-type]


def test_sensor_perfecto() -> None:
    quiet = _stats(edges=0)
    motion = _stats(edges=3, pulses=3, holds_ms=[2700, 2700, 2700], duty_pct=13.5)
    findings = evaluate(quiet, motion)
    levels = [f.level for f in findings]
    assert Level.FAIL not in levels
    assert Level.WARN not in levels
    assert any("Tx" in f.title for f in findings)
    assert "correctamente ajustado" in recommendation(findings)


def test_demasiados_falsos_positivos() -> None:
    quiet = _stats(edges=12, duration_s=30.0)
    findings = evaluate(quiet, None)
    assert findings[0].level is Level.FAIL
    assert "Sx" in findings[0].detail


def test_sin_deteccion() -> None:
    motion = _stats(edges=0, pulses=0)
    findings = evaluate(None, motion)
    assert any(f.level is Level.FAIL for f in findings)
    assert "Requiere atencion" in recommendation(findings)


def test_tx_demasiado_alto() -> None:
    motion = _stats(edges=2, pulses=2, holds_ms=[20000, 20000], duty_pct=60.0)
    findings = evaluate(None, motion)
    assert any(f.level is Level.WARN and "Tx" in f.title for f in findings)


def test_ruido_electrico_se_reporta() -> None:
    quiet = _stats(edges=0, bounces=3)
    findings = evaluate(quiet, None)
    assert any("Ruido electrico" in f.title for f in findings)


def test_saturacion_por_duty_alto() -> None:
    motion = _stats(edges=5, pulses=5, holds_ms=[5000] * 5, duty_pct=92.0)
    findings = evaluate(None, motion)
    assert any("saturado" in f.title.lower() for f in findings)


def test_sin_datos() -> None:
    assert evaluate(None, None) == []
    assert "Sin datos" in recommendation([])
