"""Tarjeta viva del sensor de presencia.

El HC-SR501 es digital: solo dice «hay movimiento» o «no lo hay». No hay
distancia ni nivel analogico que dibujar, y fingir una escala que no existe
seria mentir con graficos. Lo que si se puede mostrar, y es lo que se hace
aqui:

* la placa, dibujada como lo que es, con un LED que destella con cada latido:
  prueba visual de que el enlace sigue vivo cada dos segundos;
* ondas alrededor del sensor cuando detecta movimiento;
* barras de actividad que suben al detectar y bajan despacio, para que un
  pulso de 2,7 s se vea aunque dure poco;
* una franja de los ultimos 60 s: la historia de lo que ha visto el sensor,
  con la alarma armada o no;
* el tiempo desde la ultima deteccion y el porcentaje de actividad.

La logica de muestreo (`SensorActivity`) no depende de Qt para poder probarla
sin abrir una ventana.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from . import theme

#: Cada cuanto se guarda una muestra de la franja temporal.
SAMPLE_S = 0.5
#: Cuantos segundos de historia se dibujan.
WINDOW_S = 60.0
#: Decaimiento de las barras: sube rapido, baja en unos 2,5 s.
RISE_S = 0.15
FALL_S = 2.5
#: Cuanto dura el destello del LED de latido.
BEAT_S = 0.35


class SensorActivity:
    """Muestras de la salida del PIR y estadisticas derivadas.

    Se guarda un booleano por cada medio segundo: suficiente para ver el pulso
    tipico (unos 2,7 s) y barato para 60 s de historia.
    """

    def __init__(
        self,
        *,
        window_s: float = WINDOW_S,
        sample_s: float = SAMPLE_S,
    ) -> None:
        self._sample_s = sample_s
        self._capacity = max(1, int(round(window_s / sample_s)))
        self._samples: deque[tuple[float, bool]] = deque(maxlen=self._capacity)
        self._next: float | None = None
        self._last_motion: float | None = None
        self._pulses = 0

    # -- Entrada -----------------------------------------------------------

    def reset(self) -> None:
        self._samples.clear()
        self._next = None
        self._last_motion = None
        self._pulses = 0

    def tick(self, now: float, level: bool) -> None:
        """Avanza la rejilla de muestras hasta `now` con el nivel actual."""
        if level:
            self._last_motion = now
        if self._next is None:
            self._next = now
        # Si el reloj salta mucho (equipo suspendido), no se rellena de golpe
        # una hora de muestras: se descarta lo viejo y se empieza de nuevo.
        guard = 0
        while now >= self._next and guard < self._capacity * 2:
            self._samples.append((self._next, level))
            self._next += self._sample_s
            guard += 1
        if guard >= self._capacity * 2:
            self._next = now + self._sample_s
        self._prune(now)

    def record_motion(self, now: float) -> None:
        """Marca una deteccion sin esperar al siguiente muestreo."""
        self._last_motion = now
        self._pulses += 1
        if self._samples:
            self._samples[-1] = (self._samples[-1][0], True)

    # -- Salida ------------------------------------------------------------

    def samples(self) -> tuple[bool, ...]:
        return tuple(level for _ts, level in self._samples)

    def duty(self, now: float, window_s: float = 10.0) -> float:
        """Porcentaje de los ultimos `window_s` segundos con movimiento."""
        since = now - window_s
        inside = [level for ts, level in self._samples if ts >= since]
        if not inside:
            return 0.0
        return 100.0 * sum(1 for level in inside if level) / len(inside)

    def last_motion_age(self, now: float) -> float | None:
        if self._last_motion is None:
            return None
        return max(0.0, now - self._last_motion)

    @property
    def pulses(self) -> int:
        return self._pulses

    def _prune(self, now: float) -> None:
        limit = now - self._capacity * self._sample_s - self._sample_s
        while self._samples and self._samples[0][0] < limit:
            self._samples.popleft()


class SensorPanel(QWidget):
    """Ilustracion del Arduino, del PIR y de su actividad reciente."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.activity = SensorActivity()
        self._connected = False
        self._level = False
        self._energy = 0.0
        self._beat = 0.0
        self._last_tick: float | None = None
        self.setMinimumHeight(196)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setToolTip(
            "Estado del sensor de presencia. Las ondas y las barras marcan movimiento en "
            "vivo; la franja inferior resume los últimos 60 segundos. El HC-SR501 solo "
            "distingue «movimiento» y «reposo»: no mide distancia."
        )

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._animate)
        self._timer.stop()

    # -- Entrada -----------------------------------------------------------

    def set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        if not connected:
            self._level = False
            self._energy = 0.0
            self._beat = 0.0
            self._last_tick = None
        self._sync_timer()
        self.update()

    def set_level(self, level: bool, *, now: float | None = None) -> None:
        """Nivel actual del PIR. Marca deteccion al pasar de reposo a movimiento."""
        stamp = time.time() if now is None else now
        if level and not self._level:
            self.activity.record_motion(stamp)
        self._level = level
        self._advance(stamp)
        self.update()

    def simulate(self, *, now: float | None = None, energy: float = 0.85) -> None:
        """Rellena una actividad de ejemplo.

        Solo para capturas y demostraciones: deja la franja de 60 s y las
        barras con movimiento reciente sin necesidad de una placa.
        """
        stamp = time.time() if now is None else now
        self._connected = True
        self.activity.reset()
        start = stamp - WINDOW_S
        self.activity.tick(start, False)
        for index in range(int(WINDOW_S / SAMPLE_S)):
            self.activity.tick(start + index * SAMPLE_S, index % 14 in (3, 4, 5))
        self._level = True
        self.activity.record_motion(stamp - 2.0)
        self._energy = max(0.0, min(1.0, energy))
        self._beat = stamp
        self._last_tick = stamp
        self._sync_timer()
        self.update()

    def beat(self, *, now: float | None = None) -> None:
        """Destello del LED: acaba de llegar un latido de la placa."""
        self._beat = time.time() if now is None else now
        self._sync_timer()
        self.update()

    def tick(self, now: float | None = None) -> None:
        """Avanza la animacion y la franja temporal (lo llama el refresco)."""
        self._advance(time.time() if now is None else now)
        self.update()

    def reset(self) -> None:
        self.activity.reset()
        self._level = False
        self._energy = 0.0
        self._beat = 0.0
        self._last_tick = None
        self.update()

    @property
    def level(self) -> bool:
        return self._level

    @property
    def energy(self) -> float:
        """Actividad suavizada, de 0 a 1, la que pintan las barras."""
        return self._energy

    # -- Animacion ---------------------------------------------------------

    def _animate(self) -> None:
        self._advance(time.time())
        self.update()

    def _advance(self, now: float) -> None:
        previous = self._last_tick
        self._last_tick = now
        dt = 0.0 if previous is None else min(max(now - previous, 0.0), 1.0)
        target = 1.0 if self._level else 0.0
        speed = RISE_S if target > self._energy else FALL_S
        if dt > 0.0:
            step = dt / speed
            if self._energy < target:
                self._energy = min(target, self._energy + step)
            else:
                self._energy = max(target, self._energy - step)
        self.activity.tick(now, self._level)
        self._sync_timer()

    def _sync_timer(self) -> None:
        """La animacion solo corre si hay algo que animar."""
        busy = self._connected or self._level or self._energy > 0.01
        if busy and not self._timer.isActive():
            self._timer.start()
        elif not busy and self._timer.isActive():
            self._timer.stop()

    # -- Pintura -----------------------------------------------------------

    def paintEvent(self, event: object) -> None:  # noqa: N802 - API de Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = float(self.width())
        height = float(self.height())

        board_w = min(150.0, max(104.0, width * 0.40))
        board_h = min(84.0, max(62.0, height * 0.40))
        board = QRectF(0.0, 2.0, board_w, board_h)
        self._paint_board(painter, board)
        # El sensor se separa del borde derecho lo justo para que las ondas
        # quepan dentro del widget sin recortarse.
        center = QPointF(width - 56.0, 8.0 + board_h * 0.58)
        radius = min(26.0, board_h * 0.34)
        self._paint_wires(
            painter, QPointF(board.right() - 6.0, 2.0 + board_h * 0.5), center, radius
        )
        self._paint_sensor(painter, center, radius)

        bars_y = 6.0 + board_h
        self._paint_bars(painter, QRectF(0.0, bars_y, width, 12.0))

        strip_y = bars_y + 18.0
        self._paint_strip(painter, QRectF(0.0, strip_y, width, 26.0))

        self._paint_caption(painter, QRectF(0.0, strip_y + 32.0, width, height - strip_y - 32.0))
        painter.end()

    def _paint_board(self, painter: QPainter, rect: QRectF) -> None:
        base = QColor(theme.BOARD if self._connected else theme.BOARD_OFF)
        dark = QColor(theme.BOARD_DARK if self._connected else theme.LINE)
        path = QPainterPath()
        path.addRoundedRect(rect, 8.0, 8.0)
        painter.setPen(QPen(dark, 1.4))
        painter.setBrush(base)
        painter.drawPath(path)

        inset = rect.adjusted(6.0, 6.0, -6.0, -6.0)

        # Conector USB y jack de alimentacion, los dos arriba a la izquierda.
        usb = QRectF(inset.left(), inset.top(), inset.width() * 0.26, inset.height() * 0.30)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.SILVER))
        painter.drawRoundedRect(usb, 2.0, 2.0)
        jack_r = min(inset.height() * 0.16, 9.0)
        painter.setBrush(QColor(theme.BOARD_CHIP))
        painter.drawEllipse(
            QPointF(inset.left() + inset.width() * 0.42, inset.top() + jack_r),
            jack_r,
            jack_r,
        )

        # Microcontrolador.
        chip_w = inset.width() * 0.22
        chip_h = inset.height() * 0.34
        chip = QRectF(
            inset.right() - chip_w - inset.width() * 0.06,
            inset.top() + inset.height() * 0.30,
            chip_w,
            chip_h,
        )
        painter.setBrush(QColor(theme.BOARD_CHIP))
        painter.drawRoundedRect(chip, 1.5, 1.5)

        # Cabeceras de pines: dos filas de almohadillas doradas.
        painter.setBrush(QColor(theme.GOLD if self._connected else theme.SILVER))
        pads = 7
        pad_w = inset.width() / (pads * 1.7)
        for index in range(pads):
            x = inset.left() + index * pad_w * 1.6
            painter.drawRoundedRect(QRectF(x, inset.top(), pad_w, 3.0), 1.0, 1.0)
            painter.drawRoundedRect(QRectF(x, inset.bottom() - 3.0, pad_w, 3.0), 1.0, 1.0)

        # Serigrafia y LED de latido.
        font = QFont(theme.FONT_FAMILY, 6)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff" if self._connected else theme.MUTED))
        painter.drawText(
            QRectF(inset.left(), inset.bottom() - inset.height() * 0.34, inset.width(), 12.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            "ARDUINO MEGA",
        )

        glow = 0.0
        if self._connected and self._beat > 0.0:
            age = max(0.0, time.time() - self._beat)
            if age <= BEAT_S:
                glow = 1.0 - age / BEAT_S
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.OK if glow > 0.25 else theme.LINE))
        painter.drawEllipse(
            QPointF(inset.left() + 4.0, inset.top() + inset.height() * 0.62),
            3.5,
            3.5,
        )

    def _paint_wires(
        self, painter: QPainter, start: QPointF, center: QPointF, radius: float
    ) -> None:
        """Tres cables del PIR a la placa: llena el hueco y explica el conjunto."""
        end = QPointF(center.x() - radius * 0.7, center.y() + radius * 0.72)
        colors = (
            ("#d9534f", "#2b2f33", "#e6b422")
            if self._connected
            else (
                theme.SILVER,
                theme.SILVER,
                theme.SILVER,
            )
        )
        path = QPainterPath(start)
        bend = (end.x() - start.x()) * 0.55
        path.cubicTo(
            QPointF(start.x() + bend, start.y() + 10.0),
            QPointF(end.x() - bend, end.y() + 12.0),
            end,
        )
        for index, color in enumerate(colors):
            painter.save()
            painter.translate(0.0, index * 3.0 - 3.0)
            pen = QPen(QColor(color), 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.restore()

    def _paint_sensor(self, painter: QPainter, center: QPointF, radius: float) -> None:
        radius = max(18.0, radius)
        active = self._level and self._connected
        color = QColor(theme.OK if active else (theme.MUTED if self._connected else theme.LINE))

        # Ondas concentricas: solo mientras dura el pulso, con desvanecido.
        # Se aplanan en vertical para que no invadan las barras de actividad.
        if active:
            phase = math.sin(time.time() * 4.0) / 2.0 + 0.5
            for index in range(3):
                spread = (phase + index / 3.0) % 1.0
                ring = radius * (1.0 + spread * 0.95)
                alpha = int(120 * (1.0 - spread))
                if alpha <= 0:
                    continue
                ring_color = QColor(theme.OK)
                ring_color.setAlpha(alpha)
                painter.setPen(QPen(ring_color, 2.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(center, ring, ring * 0.62)

        # Cupula del HC-SR501.
        dome = QPainterPath()
        dome.moveTo(center.x() - radius, center.y() + radius * 0.55)
        dome.arcTo(
            QRectF(
                center.x() - radius,
                center.y() - radius * 1.45,
                radius * 2.0,
                radius * 2.0,
            ),
            180.0,
            -180.0,
        )
        dome.closeSubpath()
        painter.setPen(QPen(color.darker(120), 1.4))
        painter.setBrush(QColor(color.red(), color.green(), color.blue(), 60))
        painter.drawPath(dome)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(center, radius * 0.42, radius * 0.42)

        # Base de la placa del sensor.
        base = QRectF(
            center.x() - radius * 1.05,
            center.y() + radius * 0.55,
            radius * 2.1,
            max(5.0, radius * 0.28),
        )
        painter.setBrush(QColor(theme.BOARD if self._connected else theme.BOARD_OFF))
        painter.drawRoundedRect(base, 2.0, 2.0)

    def _paint_bars(self, painter: QPainter, rect: QRectF) -> None:
        segments = 6
        gap = 4.0
        segment_w = max(4.0, (rect.width() - gap * (segments - 1)) / segments)
        filled = self._energy * segments
        painter.setPen(Qt.PenStyle.NoPen)
        for index in range(segments):
            x = rect.left() + index * (segment_w + gap)
            piece = QRectF(x, rect.top(), segment_w, rect.height())
            level = filled - index
            if level <= 0.02 or not self._connected:
                painter.setBrush(QColor(theme.LINE))
            else:
                color = QColor(theme.OK)
                color.setAlpha(int(90 + 165 * min(1.0, level)))
                painter.setBrush(color)
            painter.drawRoundedRect(piece, 3.0, 3.0)

    def _paint_strip(self, painter: QPainter, rect: QRectF) -> None:
        samples = self.activity.samples()
        capacity = max(1, int(round(WINDOW_S / SAMPLE_S)))
        cell_w = max(1.0, rect.width() / capacity)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f2f3f5"))
        painter.drawRoundedRect(rect, 4.0, 4.0)

        offset = capacity - len(samples)
        painter.setBrush(QColor(theme.OK if self._connected else theme.MUTED))
        for index, level in enumerate(samples):
            if not level:
                continue
            x = rect.left() + (offset + index) * cell_w
            painter.drawRect(
                QRectF(x, rect.top() + 1.0, max(1.0, cell_w - 0.5), rect.height() - 2.0)
            )

        painter.setPen(QPen(QColor(theme.LINE), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 4.0, 4.0)

    def _paint_caption(self, painter: QPainter, rect: QRectF) -> None:
        now = time.time()
        if not self._connected:
            state = "Sin enlace"
            color = theme.MUTED
        elif self._level:
            state = "MOVIMIENTO"
            color = theme.OK
        else:
            state = "Reposo"
            color = theme.TEXT

        state_font = QFont(theme.FONT_FAMILY, 10)
        state_font.setBold(True)
        painter.setFont(state_font)
        painter.setPen(QColor(color))
        painter.drawText(
            QRectF(rect.left(), rect.top(), rect.width(), 18.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            state,
        )

        if not self._connected:
            detail = "Conecta la placa para ver la actividad del sensor."
        else:
            age = self.activity.last_motion_age(now)
            since = "sin detecciones todavía" if age is None else f"última hace {age:.0f} s"
            duty = self.activity.duty(now)
            detail = f"{since} · actividad {duty:.0f} %"

        detail_font = QFont(theme.FONT_FAMILY, 8)
        painter.setFont(detail_font)
        painter.setPen(QColor(theme.MUTED))
        painter.drawText(
            QRectF(rect.left(), rect.top() + 18.0, rect.width(), 16.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            detail,
        )


__all__ = ["BEAT_S", "SAMPLE_S", "SensorActivity", "SensorPanel", "WINDOW_S"]
