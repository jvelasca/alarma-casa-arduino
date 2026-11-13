"""Modo de ajuste del sensor.

Los potenciometros del HC-SR501 son fisicos: ningun software puede girarlos.
Lo que si se puede hacer, y es lo que hace este modulo, es **medir** lo que el
sensor ve y traducirlo a instrucciones concretas sobre que potenciometro mover
y en que direccion.

Se mide en dos fases, porque cada potenciometro se manifiesta de forma distinta:

* ``quiet``  (nadie se mueve): mide falsos positivos. Habla de ``Sx``.
* ``motion`` (el usuario cruza la estancia): mide detecciones y duracion del
  pulso. Habla de alcance (``Sx``) y de retencion (``Tx``).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from enum import StrEnum

from ..protocol.messages import CalibDone, CalibEdge, CalibWindow, Message

# Duracion por debajo de la cual consideramos que el pulso es el minimo del
# modulo y no una eleccion del usuario.
MIN_HOLD_MS = 3200
HOLD_UNIFORM_MS = 120  # dispersion maxima para considerar el pulso constante
HIGH_HOLD_MS = 15000
SATURATED_DUTY_PCT = 85.0


class Level(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Finding:
    level: Level
    title: str
    detail: str


@dataclass(slots=True)
class SessionStats:
    phase: str
    duration_s: float = 0.0
    edges: int = 0
    pulses: int = 0
    holds_ms: list[int] = field(default_factory=list)
    high_ms: int = 0
    duty_pct: float = 0.0
    bounces: int = 0
    min_gap_ms: int = 0
    max_gap_ms: int = 0

    @property
    def edges_per_min(self) -> float:
        if self.duration_s <= 0:
            return 0.0
        return self.edges * 60.0 / self.duration_s

    @property
    def hold_avg_ms(self) -> int:
        return int(statistics.fmean(self.holds_ms)) if self.holds_ms else 0

    @property
    def hold_spread_ms(self) -> int:
        if len(self.holds_ms) < 2:
            return 0
        return max(self.holds_ms) - min(self.holds_ms)

    @property
    def hold_is_uniform(self) -> bool:
        return bool(self.holds_ms) and self.hold_spread_ms <= HOLD_UNIFORM_MS


class Calibrator:
    """Acumula una fase de calibracion a partir de los mensajes del dispositivo."""

    def __init__(self, phase: str = "quiet") -> None:
        self.phase = phase
        self.reset(phase)

    def reset(self, phase: str | None = None) -> None:
        if phase is not None:
            self.phase = phase
        self._last_rise_ms: int | None = None
        self._open_pulse_start: int | None = None
        self._holds: list[int] = []
        self._edges = 0
        self._bounces = 0
        self._duration_ms = 0
        self._high_ms = 0
        self._duty = 0.0
        self._min_gap = 0
        self._max_gap = 0
        self._done = False

    @property
    def finished(self) -> bool:
        return self._done

    def feed(self, message: Message) -> None:
        if isinstance(message, CalibEdge):
            self._on_edge(message)
        elif isinstance(message, CalibDone):
            self._on_done(message)
        elif isinstance(message, CalibWindow):
            self._bounces = max(self._bounces, message.bounces)

    def _on_edge(self, edge: CalibEdge) -> None:
        if edge.level:
            self._edges += 1
            if self._last_rise_ms is not None:
                gap = edge.ms - self._last_rise_ms
                self._min_gap = gap if not self._min_gap else min(self._min_gap, gap)
                self._max_gap = max(self._max_gap, gap)
            self._last_rise_ms = edge.ms
            self._open_pulse_start = edge.ms
        else:
            if self._open_pulse_start is not None:
                self._holds.append(edge.ms - self._open_pulse_start)
                self._open_pulse_start = None

    def _on_done(self, done: CalibDone) -> None:
        self._done = True
        self._duration_ms = done.elapsed_ms
        self._high_ms = done.high_ms
        self._duty = done.duty_pct
        self._bounces = max(self._bounces, done.bounces)
        if done.min_gap_ms:
            self._min_gap = done.min_gap_ms
        if done.max_gap_ms:
            self._max_gap = done.max_gap_ms
        # Los totales del dispositivo mandan sobre lo deducido por los flancos.
        if done.edges and done.edges != self._edges:
            self._edges = done.edges

    def stats(self) -> SessionStats:
        return SessionStats(
            phase=self.phase,
            duration_s=round(self._duration_ms / 1000.0, 1),
            edges=self._edges,
            pulses=len(self._holds),
            holds_ms=list(self._holds),
            high_ms=self._high_ms,
            duty_pct=self._duty,
            bounces=self._bounces,
            min_gap_ms=self._min_gap,
            max_gap_ms=self._max_gap,
        )


# ---------------------------------------------------------------------------
# Diagnostico
# ---------------------------------------------------------------------------


def evaluate(quiet: SessionStats | None, motion: SessionStats | None) -> list[Finding]:
    """Traduce las mediciones a instrucciones sobre los potenciometros."""
    findings: list[Finding] = []

    if quiet is not None:
        if quiet.edges == 0:
            findings.append(
                Finding(
                    Level.OK,
                    "Sin falsos positivos en reposo",
                    f"0 detecciones en {quiet.duration_s:.0f} s. La sensibilidad "
                    "(Sx) no esta disparando sola.",
                )
            )
        elif quiet.edges <= 2:
            findings.append(
                Finding(
                    Level.WARN,
                    "Alguna deteccion en reposo",
                    f"{quiet.edges} detecciones en {quiet.duration_s:.0f} s "
                    f"({quiet.edges_per_min:.1f}/min). Puede ser una mascota, una "
                    "corriente de aire o el sensor algo alto de sensibilidad.",
                )
            )
        else:
            findings.append(
                Finding(
                    Level.FAIL,
                    "Demasiadas detecciones en reposo",
                    f"{quiet.edges} detecciones en {quiet.duration_s:.0f} s "
                    f"({quiet.edges_per_min:.1f}/min). Gira Sx hacia el minimo y "
                    "revisa que la lente no apunte a una ventana, a una rejilla de "
                    "aire o a una fuente de calor.",
                )
            )

        if quiet.bounces > 0:
            findings.append(
                Finding(
                    Level.WARN,
                    "Ruido electrico en el enlace",
                    f"{quiet.bounces} flancos descartados por el antirrebote. Suele "
                    "indicar un rail de 5 V bajo o un cable largo sin apantallar: "
                    "revisa la alimentacion antes de tocar Sx.",
                )
            )

    if motion is not None:
        if motion.pulses == 0:
            findings.append(
                Finding(
                    Level.FAIL,
                    "No detecta movimiento",
                    "Ninguna deteccion durante la prueba. Comprueba en este orden: "
                    "1) que OUT este en D2 y la masa comun; 2) que Sx no este al "
                    "minimo, gira hacia el maximo; 3) acercate a menos de 3 m y "
                    "cruza de lado, no de frente.",
                )
            )
        else:
            findings.append(
                Finding(
                    Level.OK,
                    "Detecta movimiento",
                    f"{motion.pulses} pulsos en {motion.duration_s:.0f} s. "
                    f"Alcance suficiente para la ubicacion actual.",
                )
            )

            if motion.hold_is_uniform and motion.hold_avg_ms <= MIN_HOLD_MS:
                findings.append(
                    Finding(
                        Level.INFO,
                        "Tx practicamente al minimo",
                        f"El pulso dura {motion.hold_avg_ms} ms en todos los casos "
                        f"(dispersion {motion.hold_spread_ms} ms). Es el minimo del "
                        "modulo, no una eleccion. Si prefieres pulsos mas largos, "
                        "gira Tx en sentido horario. Para la alarma conviene dejarlo "
                        "asi: el tiempo relevante lo gestiona el PC.",
                    )
                )
            elif motion.hold_avg_ms > HIGH_HOLD_MS:
                findings.append(
                    Finding(
                        Level.WARN,
                        "Tx demasiado alto",
                        f"El pulso dura {motion.hold_avg_ms} ms. Dos personas "
                        "separadas menos de ese tiempo se funden en un solo evento. "
                        "Gira Tx hacia el minimo.",
                    )
                )
            else:
                findings.append(
                    Finding(
                        Level.INFO,
                        "Tx en posicion intermedia",
                        f"El pulso dura {motion.hold_avg_ms} ms "
                        f"(min {min(motion.holds_ms)}, max {max(motion.holds_ms)}).",
                    )
                )

            if motion.duty_pct >= SATURATED_DUTY_PCT:
                findings.append(
                    Finding(
                        Level.WARN,
                        "Sensor saturado",
                        f"La salida estuvo alta el {motion.duty_pct:.1f} % del "
                        "tiempo. Si no estabas en movimiento continuo, Sx esta "
                        "demasiado alto.",
                    )
                )

    return findings


def recommendation(findings: list[Finding]) -> str:
    """Resumen en una linea para no obligar a leer todo el informe."""
    failures = [f for f in findings if f.level is Level.FAIL]
    warnings = [f for f in findings if f.level is Level.WARN]
    if failures:
        return "Requiere atencion: " + "; ".join(f.title.lower() for f in failures)
    if warnings:
        return "Aceptable con reservas: " + "; ".join(f.title.lower() for f in warnings)
    if findings:
        return "Sensor correctamente ajustado para esta ubicacion."
    return "Sin datos suficientes."
