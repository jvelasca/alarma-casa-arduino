"""Ventana de prueba y ajuste del zumbador.

El zumbador es la unica pieza del montaje que se comprueba con el oido: no hay
lectura que diga si suena. Por eso esta ventana junta las tres cosas que hacen
falta para dejarlo fino —verificar el cableado, elegir la frecuencia en la que
el piezo suena mas fuerte y fijar cuanto dura la sirena— en vez de repartirlas
entre el menu y un fichero de configuracion.

Las pruebas se envian a la placa con `force=1`: durante el calentamiento del PIR
el firmware silencia los pitidos automaticos, y sin esa excepcion el boton de
prueba recien conectado parecia un cableado roto.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QWidget,
)

from ...config import AppConfig, BuzzerConfig
from .. import theme
from .common import Dialog

# Patrones del firmware: (nombre, titulo, cuando suena en el uso normal).
PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("test", "Secuencia de prueba", "Tres tonos ascendentes. Es la prueba rápida del menú."),
    ("armed", "Alarma armada", "Dos pitidos al armar desde la ventana."),
    ("disarmed", "Alarma desarmada", "Un tono grave al desarmar."),
    ("detect", "Detección de movimiento", "Tres pitidos cortos con la alarma armada."),
    ("alarm", "Sirena de alarma", "Sirena alterna mientras dure la alarma."),
    ("error", "Error", "Tono grave largo, para fallos del enlace."),
)

# La sirena es el unico patron que gasta el `siren_ms` configurado.
_SIREN_PATTERN = "alarm"

#: Que dice la ventana cuando la prueba no sale.
_REFUSED = "No se pudo probar: revisa la conexión y que el zumbador esté activado."


class BuzzerDialog(Dialog):
    """Prueba patrones y tonos, y guarda el ajuste del zumbador.

    `on_pattern` y `on_tone` reciben el estado actual de la casilla de activado
    para que la prueba suene aunque el cambio aun no se haya guardado.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        config: AppConfig,
        on_save: Callable[[AppConfig], None],
        on_pattern: Callable[[str, int, bool], bool],
        on_tone: Callable[[int, int, bool], bool],
        connected: bool = False,
        warming: bool = False,
        device_pin: int | None = None,
    ) -> None:
        super().__init__(
            parent,
            title="Zumbador",
            subtitle=(
                "Comprueba el cableado con un tono continuo o con cada patrón, elige la "
                "frecuencia que mejor suena y guarda cuánto dura la sirena."
            ),
            icon_name="bell",
            icon_color=theme.ACCENT,
        )
        self._config = config
        self._on_save = on_save
        self._on_pattern = on_pattern
        self._on_tone = on_tone
        self._connected = connected

        buzzer = config.buzzer
        pin = device_pin if device_pin is not None else buzzer.pin

        self._enabled = QCheckBox("Pitidos locales en la placa")
        self._enabled.setChecked(buzzer.enabled)
        self._enabled.setToolTip(
            "Interruptor del zumbador en el Arduino. La voz del PC y los avisos por correo o "
            "SMS no dependen de él."
        )
        self.add_row(
            "Zumbador",
            self._enabled,
            "Si se desmarca, la placa deja de pitar aunque le llegue la orden.",
        )

        wiring = QLabel(self._wiring_text(pin, device_pin))
        wiring.setObjectName("muted")
        wiring.setFont(theme.font(8))
        wiring.setWordWrap(True)
        wiring.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        wiring.setContentsMargins(0, 0, 0, 6)
        self.add_full_row(wiring)

        self._siren = QSpinBox()
        self._siren.setRange(1, 600)
        self._siren.setSuffix(" s")
        self._siren.setValue(max(1, min(600, round(buzzer.siren_ms / 1000))))
        self._siren.setToolTip(
            "Cuánto tiempo pita la placa al dispararse la alarma. 120 s (dos minutos) es "
            "un buen punto: da margen a reaccionar sin dejarla sonando toda la tarde."
        )
        self.add_row(
            "Sirena",
            self._siren,
            "El piezo pita solo este tiempo y se calla: la placa acota entre 1 y 600 s "
            "(10 min) aunque reciba otra cosa.",
        )

        self._hz = QSpinBox()
        self._hz.setRange(100, 8000)
        self._hz.setSingleStep(50)
        self._hz.setSuffix(" Hz")
        self._hz.setValue(buzzer.test_hz)
        self._hz.setToolTip(
            "Los piezo baratos resuenan entre 2 y 4 kHz: fuera de esa banda el mismo zumbador "
            "suena mucho más flojo."
        )
        self.add_row("Frecuencia de prueba", self._hz, "Sube o baja hasta oír el punto más fuerte.")

        self._tone_ms = QSpinBox()
        self._tone_ms.setRange(100, 10_000)
        self._tone_ms.setSingleStep(100)
        self._tone_ms.setSuffix(" ms")
        self._tone_ms.setValue(buzzer.test_ms)
        self._tone_ms.setToolTip("Cuánto dura el tono continuo de comprobación.")
        self.add_row(
            "Duración del tono", self._tone_ms, "Dos segundos bastan para oírlo sin molestar."
        )

        self._pattern = QComboBox()
        for name, title, hint in PATTERNS:
            self._pattern.addItem(title, name)
            self._pattern.setItemData(self._pattern.count() - 1, hint, Qt.ItemDataRole.ToolTipRole)
        self._play_button = QPushButton("Reproducir")
        self._play_button.setToolTip("Envía a la placa el patrón seleccionado.")
        self._play_button.clicked.connect(self._play_pattern)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        row_layout.addWidget(self._pattern, stretch=1)
        row_layout.addWidget(self._play_button)
        self.add_row("Patrón", row, "El mismo pitido que hará la placa en cada situación.")

        self._feedback = QLabel()
        self._feedback.setObjectName("muted")
        self._feedback.setFont(theme.font(8))
        self._feedback.setWordWrap(True)
        self.add_full_row(self._feedback)

        self._tone_button = self.add_left_button(
            "Tono continuo",
            self._play_tone,
            "Suena sin interrupción durante la duración indicada. Es la forma más rápida de "
            "saber si el zumbador está bien conectado.",
        )
        self.add_standard_buttons()

        if not connected:
            self._set_test_enabled(False)
            self._feedback.setText(
                "Sin conexión con la placa: conecta el Arduino para poder probar el zumbador."
            )
        elif warming:
            self._feedback.setText(
                "El PIR está calentando. Los pitidos automáticos siguen silenciados, pero estas "
                "pruebas se fuerzan y suenan igual."
            )

    # -- Estado ------------------------------------------------------------

    def _wiring_text(self, pin: int, device_pin: int | None) -> str:
        """Texto de cableado. El pin bueno es el que anuncia la placa."""
        donde = f"D{pin}"
        if device_pin is not None and device_pin != self._config.buzzer.pin:
            donde += f" (la placa anuncia D{device_pin}; manda el firmware)"
        return (
            f"Zumbador pasivo entre {donde} y el GND contiguo a D13. "
            "Pata larga al pin (positivo) y pata corta a GND (negativo). "
            "Un piezo pasivo suele sonar también del revés, solo algo más flojo: si no se oye "
            "nada, revisa que sea pasivo (los activos con oscilador interno suenan mal aquí)."
        )

    def _set_test_enabled(self, enabled: bool) -> None:
        for widget in (
            self._tone_button,
            self._play_button,
            self._pattern,
            self._hz,
            self._tone_ms,
        ):
            widget.setEnabled(enabled)

    # -- Pruebas -----------------------------------------------------------

    def _siren_ms(self) -> int:
        return self._siren.value() * 1000

    def _play_tone(self) -> None:
        if not self._connected:
            return
        hz, ms = self._hz.value(), self._tone_ms.value()
        if self._on_tone(hz, ms, self._enabled.isChecked()):
            self._feedback.setText(f"Tono de {hz} Hz durante {ms} ms enviado.")
        else:
            self._feedback.setText(_REFUSED)

    def _play_pattern(self) -> None:
        if not self._connected:
            return
        name = str(self._pattern.currentData())
        title = self._pattern.currentText()
        ms = self._siren_ms() if name == _SIREN_PATTERN else 0
        if self._on_pattern(name, ms, self._enabled.isChecked()):
            self._feedback.setText(f"Patrón «{title}» enviado.")
        else:
            self._feedback.setText(_REFUSED)

    # -- Guardado ----------------------------------------------------------

    def build_config(self) -> AppConfig:
        buzzer = BuzzerConfig(
            enabled=self._enabled.isChecked(),
            pin=self._config.buzzer.pin,
            siren_ms=self._siren_ms(),
            test_hz=self._hz.value(),
            test_ms=self._tone_ms.value(),
        )
        return replace(self._config, buzzer=buzzer)

    def accept(self) -> None:
        try:
            self._on_save(self.build_config())
        except Exception as exc:  # noqa: BLE001 - el fallo se muestra, no se propaga
            QMessageBox.critical(
                self, "No se pudo guardar", f"La configuración no se ha guardado:\n{exc}"
            )
            return
        super().accept()
