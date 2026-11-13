"""Asistente de ajuste del sensor HC-SR501, en Qt.

Los potenciometros del sensor son fisicos: el programa no puede girarlos. Esta
ventana mide su efecto en dos fases y traduce las mediciones a instrucciones
concretas sobre Sx (sensibilidad y alcance) y Tx (tiempo en alta).

Todo el control de fases va por eventos del dispositivo (`calib.window`,
`calib.done`) y por temporizadores de Qt; nunca se bloquea esperando al puerto.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..device.calibrator import Calibrator, Finding, Level, SessionStats, evaluate, recommendation
from ..device.manager import DeviceManager
from ..paths import storage_root
from ..protocol.messages import CalibDone, CalibEdge, CalibWindow, Message
from . import icons, theme
from .audio import beep_async
from .bridge import QtBridge
from .widgets import Card, tip

log = logging.getLogger(__name__)

WINDOW_TITLE = "Ajuste del sensor — HC-SR501"

_LEVEL_COLOR = {
    Level.OK: theme.OK,
    Level.WARN: theme.WARN,
    Level.FAIL: theme.FAIL,
    Level.INFO: theme.ACCENT,
}

_LEVEL_TEXT = {
    Level.OK: "CORRECTO",
    Level.WARN: "AVISO",
    Level.FAIL: "FALLO",
    Level.INFO: "INFO",
}

_DATA_DIR = storage_root() / "data"


class CalibrationDialog(QDialog):
    """Cuatro pasos guiados: introduccion, reposo, movimiento y veredicto."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        manager: DeviceManager,
        bridge: QtBridge,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._bridge = bridge
        self._on_close_cb = on_close

        self._calibrator: Calibrator | None = None
        self._quiet: SessionStats | None = None
        self._motion: SessionStats | None = None
        self._phase_active = False
        self._phase_name = ""
        self._phase_deadline = 0.0
        self._findings: list[Finding] = []
        self._phase_done_cb: Callable[[], None] | None = None
        self._prep_active = False
        self._prep_remaining = 0
        # Duraciones elegidas en la introduccion. Se guardan como enteros y no
        # se leen de los `QSpinBox` cuando hacen falta: `_reset()` destruye
        # esos widgets al cambiar de paso, y consultarlos despues de la cuenta
        # atras lanzaba un RuntimeError dentro de un slot, que en PyQt aborta la
        # aplicacion entera. Guardarlas aqui ademas conserva el ajuste al
        # pulsar «Repetir».
        self._durations = {"prep": 30, "quiet": 45, "motion": 45}
        # Identifica la fase en curso para que un `calib.stop` lento no cierre
        # la fase siguiente.
        self._phase_token = 0

        self.setWindowTitle(WINDOW_TITLE)
        self.setModal(False)
        self.resize(720, 640)
        self.setMinimumSize(620, 540)

        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)

        heading = QLabel("Ajuste del sensor")
        heading.setFont(theme.font(15, bold=True))
        outer.addWidget(heading)

        subtitle = QLabel(
            "Los potenciómetros son físicos: el programa no puede girarlos. Los dos sentidos "
            "se miden por separado para saber cuál tocar.  Sx = sensibilidad y alcance  ·  "
            "Tx = tiempo que la salida sigue alta"
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        subtitle.setFont(theme.font(9))
        outer.addWidget(subtitle)

        self._body = QVBoxLayout()
        self._body.setSpacing(10)
        outer.addLayout(self._body, stretch=1)

        self._footer = QHBoxLayout()
        self._footer.setSpacing(8)
        outer.addLayout(self._footer)

        self._bridge.message.connect(self._on_calib_message)
        self._show_intro()

    # ------------------------------------------------------------------
    # Estructura dinamica
    # ------------------------------------------------------------------

    @staticmethod
    def _clear(layout: QLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item is None:
                continue
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            child = item.layout()
            if child is not None:
                CalibrationDialog._clear(child)

    def _reset(self) -> None:
        self._clear(self._body)
        self._clear(self._footer)

    def _body_card(self) -> tuple[Card, QVBoxLayout]:
        card = Card()
        self._body.addWidget(card, stretch=1)
        return card, card.body()

    def _foot(self, *buttons: QWidget, note: str = "") -> None:
        caption = QLabel(note)
        caption.setObjectName("muted")
        caption.setFont(theme.font(8))
        self._footer.addWidget(caption, stretch=1)
        for button in buttons:
            self._footer.addWidget(button)

    # ------------------------------------------------------------------
    # Paso 1: introduccion
    # ------------------------------------------------------------------

    def _show_intro(self) -> None:
        self._reset()
        card, column = self._body_card()

        heading = QLabel("Qué va a ocurrir")
        heading.setObjectName("cardTitle")
        column.addWidget(heading)

        steps = (
            ("1. Preparación", "Una cuenta atrás para que salgas de la estancia."),
            ("2. Reposo", "Nadie se mueve. Se cuentan detecciones falsas: habla de Sx."),
            ("3. Movimiento", "Cruzas la estancia varias veces: habla de alcance y de Tx."),
            ("4. Veredicto", "Qué está bien, qué tocar y hacia dónde girarlo."),
        )
        for name, detail in steps:
            row = QHBoxLayout()
            row.setSpacing(12)
            title = QLabel(name)
            title.setFont(theme.font(10, bold=True))
            title.setFixedWidth(110)
            row.addWidget(title)
            text = QLabel(detail)
            text.setObjectName("muted")
            text.setWordWrap(True)
            row.addWidget(text, stretch=1)
            column.addLayout(row)

        column.addWidget(self._separator())

        durations = QHBoxLayout()
        durations.setSpacing(10)
        self._prep_s = self._spin(
            0, 120, 5, self._durations["prep"], "Segundos antes de empezar a medir."
        )
        self._quiet_s = self._spin(
            10, 300, 5, self._durations["quiet"], "Duración de la fase de reposo."
        )
        self._motion_s = self._spin(
            10, 300, 5, self._durations["motion"], "Duración de la fase de movimiento."
        )
        for caption, widget in (
            ("Preparación", self._prep_s),
            ("Reposo", self._quiet_s),
            ("Movimiento", self._motion_s),
        ):
            block = QVBoxLayout()
            block.setSpacing(2)
            label = QLabel(caption)
            label.setObjectName("muted")
            label.setFont(theme.font(8))
            block.addWidget(label)
            block.addWidget(widget)
            durations.addLayout(block)
        durations.addStretch(1)
        column.addLayout(durations)

        hint = QLabel(
            "Si la placa lleva más de un minuto encendida no hace falta esperar el "
            "calentamiento del PIR: usa «Herramientas → Omitir calentamiento»."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        hint.setFont(theme.font(8))
        column.addWidget(hint)
        column.addStretch(1)

        start = QPushButton("Comenzar")
        start.setObjectName("primary")
        start.clicked.connect(self._start_prep)
        close = QPushButton("Cerrar")
        close.clicked.connect(self.close)
        self._foot(start, close, note="Puedes cerrar esta ventana en cualquier momento.")

    @staticmethod
    def _spin(lo: int, hi: int, step: int, value: int, tooltip: str) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.setSuffix(" s")
        spin.setToolTip(tooltip)
        return spin

    @staticmethod
    def _separator() -> QWidget:
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.LINE};")
        return line

    # ------------------------------------------------------------------
    # Paso 2: preparacion
    # ------------------------------------------------------------------

    def _start_prep(self) -> None:
        # Se copian las duraciones antes de `_reset()`: ese metodo destruye los
        # `QSpinBox` de la introduccion y leerlos despues fallaria.
        self._durations = {
            "prep": self._prep_s.value(),
            "quiet": self._quiet_s.value(),
            "motion": self._motion_s.value(),
        }
        prep = self._durations["prep"]
        if prep <= 0:
            self._start_quiet()
            return

        self._reset()
        card, column = self._body_card()
        column.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        title = QLabel("Sal de la estancia ahora")
        title.setFont(theme.font(16, bold=True))
        column.addWidget(title, alignment=Qt.AlignmentFlag.AlignHCenter)

        text = QLabel(
            "La fase de reposo solo mide falsos positivos si no hay nadie delante del "
            "sensor. Si te quedas sentado enfrente, la medición sale contaminada."
        )
        text.setObjectName("muted")
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(text)

        self._prep_label = QLabel(str(prep))
        self._prep_label.setFont(theme.font(34, bold=True))
        self._prep_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(self._prep_label)

        self._prep_bar = QProgressBar()
        self._prep_bar.setRange(0, prep)
        self._prep_bar.setValue(prep)
        self._prep_bar.setMaximumWidth(360)
        column.addWidget(self._prep_bar, alignment=Qt.AlignmentFlag.AlignHCenter)

        note = QLabel("Al terminar la cuenta atrás empieza la medición.")
        note.setObjectName("muted")
        note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(note)

        now = QPushButton("Comenzar ya")
        now.clicked.connect(self._start_quiet)
        cancel = QPushButton("Cancelar")
        cancel.clicked.connect(self.close)
        self._foot(now, cancel)

        beep_async("leave")
        self._prep_remaining = prep
        self._prep_active = True
        QTimer.singleShot(1000, self._prep_tick)

    def _prep_tick(self) -> None:
        if not self._prep_active:
            return
        self._prep_remaining -= 1
        if self._prep_remaining <= 0:
            self._prep_active = False
            self._start_quiet()
            return
        self._prep_label.setText(str(self._prep_remaining))
        self._prep_bar.setValue(self._prep_remaining)
        QTimer.singleShot(1000, self._prep_tick)

    # ------------------------------------------------------------------
    # Pasos 3 y 4: fases medidas
    # ------------------------------------------------------------------

    def _start_quiet(self) -> None:
        self._prep_active = False
        self._start_phase("quiet", float(self._durations["quiet"]), self._start_motion)

    def _start_motion(self) -> None:
        self._start_phase("motion", float(self._durations["motion"]), self._finish)

    def _start_phase(self, phase: str, seconds: float, on_done: Callable[[], None]) -> None:
        self._phase_name = phase
        self._phase_active = True
        self._phase_done_cb = on_done
        self._phase_deadline = time.monotonic() + seconds
        self._phase_seconds = seconds
        self._phase_token += 1
        self._calibrator = Calibrator(phase)
        self._high_ms = 0
        self._elapsed_ms = 0
        self._windows = 0

        self._build_phase_view(phase, seconds)
        try:
            self._manager.send("calib.start", window_ms=1000, auto_end_ms=int(seconds * 1000))
        except Exception as exc:  # noqa: BLE001 - el enlace puede caerse aqui
            QMessageBox.critical(self, "Error de enlace", f"No se pudo iniciar la fase:\n{exc}")
            self.close()
            return

        beep_async("move" if phase == "motion" else "quiet")
        self._timer.start()
        self._tick()

    def _build_phase_view(self, phase: str, seconds: float) -> None:
        self._reset()
        card, column = self._body_card()

        is_quiet = phase == "quiet"
        heading = "Fase 1 de 2 · Reposo" if is_quiet else "Fase 2 de 2 · Movimiento"
        caption = QLabel(heading)
        caption.setObjectName("cardTitle")
        column.addWidget(caption)

        instruction = (
            "No te muevas y no entres en la estancia."
            if is_quiet
            else "Cruza la estancia de lado, 3 o 4 veces, con pausas de 3 segundos."
        )
        title = QLabel(instruction)
        title.setFont(theme.font(13, bold=True))
        title.setWordWrap(True)
        column.addWidget(title)

        self._remaining_label = QLabel(f"Quedan {seconds:.0f} s")
        self._remaining_label.setObjectName("muted")
        column.addWidget(self._remaining_label)

        self._duty_bar = QProgressBar()
        self._duty_bar.setRange(0, 100)
        self._duty_bar.setValue(0)
        tip(self._duty_bar, "Porcentaje de tiempo que la salida del PIR está en alta.")
        column.addWidget(self._duty_bar)

        metrics = QHBoxLayout()
        metrics.setSpacing(18)
        self._duty_label = QLabel("Salida alta: 0,0 %")
        self._edges_label = QLabel("Detecciones: 0")
        self._gap_label = QLabel("Intervalo entre detecciones: —")
        for widget in (self._duty_label, self._edges_label, self._gap_label):
            widget.setObjectName("muted")
            metrics.addWidget(widget)
        metrics.addStretch(1)
        column.addLayout(metrics)

        self._live = QPlainTextEdit()
        self._live.setReadOnly(True)
        self._live.setFont(theme.mono(9))
        self._live.setMaximumBlockCount(400)
        tip(self._live, "Una línea por ventana de un segundo, medida por el propio Arduino.")
        column.addWidget(self._live, stretch=1)

        stop = QPushButton("Detener y ver resultado")
        stop.clicked.connect(self._stop_phase)
        self._foot(stop, note="Cada fila es una ventana de 1 s medida por el propio Arduino.")

    def _tick(self) -> None:
        if not self._phase_active:
            return
        remaining = self._phase_deadline - time.monotonic()
        self._remaining_label.setText(
            f"Quedan {max(0.0, remaining):.0f} s" if remaining > 0 else "Cerrando fase…"
        )
        if remaining < -20:
            self._timer.stop()
            QMessageBox.warning(
                self,
                "Sin respuesta",
                "El dispositivo no cerró la fase. Comprueba el enlace y vuelve a intentarlo.",
            )
            self.close()

    def _live_append(self, text: str) -> None:
        self._live.appendPlainText(text)

    def _on_calib_message(self, message: Message) -> None:
        if not self._phase_active or self._calibrator is None:
            return

        if isinstance(message, CalibWindow):
            self._calibrator.feed(message)
            self._windows += 1
            self._high_ms += message.high_ms
            self._elapsed_ms += message.elapsed_ms
            duty = min(100.0, self._high_ms * 100.0 / self._elapsed_ms) if self._elapsed_ms else 0.0
            gap = (
                f"{message.min_gap_ms / 1000:.1f} s / {message.max_gap_ms / 1000:.1f} s"
                if message.max_gap_ms
                else "—"
            )
            self._duty_bar.setValue(int(duty))
            self._duty_label.setText(f"Salida alta: {duty:.1f} %")
            self._edges_label.setText(f"Detecciones: {self._calibrator.stats().edges}")
            self._gap_label.setText(f"Intervalo entre detecciones: {gap}")

            bars = int(round(min(100.0, duty) / 100 * 24))
            self._live_append(
                f"{self._windows:3d}  [{'#' * bars}{'.' * (24 - bars)}] "
                f"{duty:5.1f} %  flancos {message.edges}"
                + (f"  rebotes {message.bounces}" if message.bounces else "")
            )

        elif isinstance(message, CalibEdge):
            self._calibrator.feed(message)

        elif isinstance(message, CalibDone):
            self._calibrator.feed(message)
            self._complete_phase()

    def _stop_phase(self) -> None:
        if not self._phase_active:
            return
        with suppress(Exception):
            self._manager.send("calib.stop")
        # Espera breve al `calib.done`; si no llega, se cierra igualmente. La
        # marca de la fase evita que este plazo, ya cumplido, cierre la fase
        # siguiente: entre medias llega el `calib.done` real y se pasa a la
        # siguiente fase, que se habria cortado a los 1,5 s.
        token = self._phase_token
        QTimer.singleShot(1500, lambda: self._force_complete(token))

    def _force_complete(self, token: int) -> None:
        if token != self._phase_token:
            return
        self._complete_phase()

    def _complete_phase(self) -> None:
        if not self._phase_active or self._calibrator is None:
            return
        self._phase_active = False
        self._timer.stop()
        stats = self._calibrator.stats()

        if self._phase_name == "quiet":
            self._quiet = stats
        else:
            self._motion = stats

        callback = self._phase_done_cb
        if callback is not None:
            callback()

    # ------------------------------------------------------------------
    # Paso 5: veredicto
    # ------------------------------------------------------------------

    def _finish(self) -> None:
        self._phase_active = False
        self._timer.stop()
        beep_async("done")

        self._findings = evaluate(self._quiet, self._motion)
        verdict = recommendation(self._findings)

        self._reset()
        summary, column = self._body_card()
        _ = summary

        worst = Level.OK
        for finding in self._findings:
            if finding.level is Level.FAIL:
                worst = Level.FAIL
                break
            if finding.level is Level.WARN:
                worst = Level.WARN

        level = QLabel(_LEVEL_TEXT.get(worst, "INFO"))
        level.setStyleSheet(f"color: {_LEVEL_COLOR.get(worst, theme.MUTED)};")
        level.setFont(theme.font(10, bold=True))
        column.addWidget(level)

        text = QLabel(verdict)
        text.setWordWrap(True)
        column.addWidget(text)

        if self._quiet is not None and self._motion is not None:
            stats = QLabel(
                f"Reposo: {self._quiet.edges} detecciones en {self._quiet.duration_s:.0f} s.   "
                f"Movimiento: {self._motion.pulses} pulsos, retención media "
                f"{self._motion.hold_avg_ms} ms."
            )
            stats.setObjectName("muted")
            stats.setWordWrap(True)
            column.addWidget(stats)

        for finding in self._findings:
            column.addWidget(self._finding_widget(finding))
        column.addStretch(1)

        save = QPushButton("Guardar informe…")
        save.setIcon(icons.icon("save", theme.ACCENT, 18))
        save.clicked.connect(self._save_report)
        repeat = QPushButton("Repetir")
        repeat.clicked.connect(self._show_intro)
        close = QPushButton("Cerrar")
        close.setObjectName("primary")
        close.clicked.connect(self.close)
        self._foot(save, repeat, close, note="El veredicto se guarda solo si pulsas «Guardar».")

    def _finding_widget(self, finding: Finding) -> QWidget:
        box = Card()
        column = box.body()
        head = QHBoxLayout()
        head.setSpacing(10)
        level = QLabel(_LEVEL_TEXT[finding.level])
        level.setStyleSheet(f"color: {_LEVEL_COLOR[finding.level]};")
        level.setFont(theme.font(9, bold=True))
        level.setFixedWidth(74)
        head.addWidget(level, alignment=Qt.AlignmentFlag.AlignTop)
        title = QLabel(finding.title)
        title.setFont(theme.font(10, bold=True))
        head.addWidget(title, stretch=1)
        column.addLayout(head)
        detail = QLabel(finding.detail)
        detail.setObjectName("muted")
        detail.setWordWrap(True)
        column.addWidget(detail)
        return box

    def _save_report(self) -> None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        default = _DATA_DIR / time.strftime("ajuste-sensor-%Y%m%d-%H%M%S.txt")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Guardar informe",
            str(default),
            "Texto (*.txt);;Todos los archivos (*)",
        )
        if not path:
            return

        lines = [
            "Informe de ajuste del sensor HC-SR501",
            time.strftime("Fecha: %Y-%m-%d %H:%M:%S"),
            "",
        ]
        for stats in (self._quiet, self._motion):
            if stats is None:
                continue
            lines += [
                f"Fase: {stats.phase}",
                f"  duracion      : {stats.duration_s:.1f} s",
                f"  detecciones   : {stats.edges}",
                f"  pulsos        : {stats.pulses}",
                f"  retencion     : {stats.hold_avg_ms} ms"
                + (f" (dispersion {stats.hold_spread_ms} ms)" if stats.holds_ms else ""),
                f"  salida alta   : {stats.duty_pct:.1f} %",
                f"  rebotes       : {stats.bounces}",
                "",
            ]
        lines.append("Diagnostico")
        for finding in self._findings:
            lines.append(f"  [{finding.level.value.upper()}] {finding.title}")
            for chunk in _wrap(finding.detail, 72):
                lines.append(f"      {chunk}")
        lines.append("")
        lines.append(f"Conclusion: {recommendation(self._findings)}")

        try:
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "No se pudo guardar", str(exc))
            return
        QMessageBox.information(self, "Informe guardado", path)

    # ------------------------------------------------------------------
    # Cierre
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802 - API de Qt
        self._timer.stop()
        self._prep_active = False
        if self._phase_active:
            with suppress(Exception):
                self._manager.send("calib.stop")
        self._phase_active = False
        with suppress(Exception):
            self._bridge.message.disconnect(self._on_calib_message)
        if self._on_close_cb is not None:
            self._on_close_cb()
        super().closeEvent(event)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
