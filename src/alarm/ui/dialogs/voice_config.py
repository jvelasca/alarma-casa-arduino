"""Ventana unica de configuracion de la voz.

Antes habia un dialogo por mensaje y otro aparte para la voz del sistema. Con
seis avisos eso son siete ventanas para comparar un campo que se repite: en vez
de recordar cuanto duraba el retardo de «disparada» al editar «sensor perdido»,
todo vive en una tabla.

La tabla tiene una fila por mensaje y una columna por decision: si se pronuncia,
que dice, que etiqueta pinta, cuantas veces, cuanto espera antes de empezar,
cuanto espera entre repeticiones y si la locucion queda en el registro. Encima,
el bloque de voz del sistema (voz, velocidad y volumen) que comparten todos.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...config import (
    DEFAULT_LABELS,
    DEFAULT_MESSAGES,
    VOICE_KEYS,
    AppConfig,
    VoiceEventConfig,
)
from ...services.tts import installed_voices
from .. import icons, theme
from .common import Dialog

# Nombre legible de cada evento en la tabla y en los menus.
EVENT_TITLES: dict[str, str] = {
    "armed": "Alarma armada",
    "disarmed": "Alarma desarmada",
    "triggered": "Alarma disparada",
    "rearmed": "Rearmada tras disparo",
    "device_lost": "Sensor perdido",
    "device_ready": "Sensor listo",
}

EVENT_ICONS: dict[str, str] = {
    "armed": "lock",
    "disarmed": "unlock",
    "triggered": "warning",
    "rearmed": "lock",
    "device_lost": "usb",
    "device_ready": "activity",
}

# Frase corta que explica cuando suena cada aviso. Va en el tooltip de la fila.
EVENT_HINTS: dict[str, str] = {
    "armed": "Suena al armar la alarma desde la ventana.",
    "disarmed": "Suena al desarmar la alarma.",
    "triggered": "Suena al detectar movimiento con la alarma armada.",
    "rearmed": "Suena cuando la alarma se rearma sola tras un disparo. Admite {n} y {max}.",
    "device_lost": "Suena si el Arduino deja de responder después de haber estado conectado.",
    "device_ready": "Suena cuando el enlace con el Arduino se recupera.",
}

COLUMNS = (
    "Evento",
    "Activo",
    "Mensaje",
    "Etiqueta",
    "Repes",
    "Retardo",
    "Intervalo",
    "Registrar",
)

# Columnas que solo tienen sentido como estado de pantalla; el resto de avisos
# no cambia la etiqueta del hero.
LABEL_KEYS = ("armed", "disarmed", "triggered")


@dataclass(slots=True)
class _Row:
    """Widgets de una fila, para leerla y escribirla sin buscar en la tabla."""

    enabled: QCheckBox
    text: QTableWidgetItem
    label: QTableWidgetItem
    repeat: QSpinBox
    delay: QDoubleSpinBox
    gap: QDoubleSpinBox
    log: QCheckBox


class VoiceConfigDialog(Dialog):
    """Tabla de todos los mensajes hablados y los ajustes de voz comunes."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        config: AppConfig,
        on_save: Callable[[AppConfig], None],
        on_try: Callable[[str, int, int, int], None] | None = None,
    ) -> None:
        super().__init__(
            parent,
            title="Configuración de voces",
            subtitle=(
                "Qué se dice en cada momento y cómo se dice. Una fila por aviso: texto, "
                "repeticiones, retardos y si queda en el registro."
            ),
            icon_name="mic",
            icon_color=theme.ACCENT,
        )
        self._config = config
        self._on_save = on_save
        self._on_try = on_try
        self.setMinimumWidth(1000)
        self._rows: dict[str, _Row] = {}
        self._updating = False

        self._build_voice_block()
        self._build_table()

        self._preview = QLabel()
        self._preview.setObjectName("muted")
        self._preview.setWordWrap(True)
        self._preview.setFont(theme.font(9))
        self._preview.setTextFormat(Qt.TextFormat.RichText)
        self.add_full_row(self._preview)

        note = QLabel(
            "Los tiempos se miden en segundos. El retardo es la espera antes de la primera "
            "palabra; el intervalo, la pausa entre una repetición y la siguiente."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        note.setFont(theme.font(8))
        self.add_full_row(note)

        self.add_left_button(
            "Probar", self._try, "Pronuncia la fila seleccionada con los valores de la tabla."
        )
        self.add_left_button(
            "Restaurar todo",
            self._reset,
            "Devuelve la tabla y la voz a los valores de fábrica.",
        )
        self.add_standard_buttons()

        self._refresh_preview()

    # -- Construccion ------------------------------------------------------

    def _build_voice_block(self) -> None:
        self._enabled = QCheckBox("Voz activada")
        self._enabled.setChecked(self._config.tts.enabled)
        self._enabled.setToolTip("Desactiva todos los mensajes hablados de una vez.")
        self.add_full_row(self._enabled)

        self._voice = QComboBox()
        self._voice.setEditable(True)
        self._voice.setToolTip(
            "Voces instaladas en Windows. Si una no aparece, escribe su nombre exacto."
        )
        self._voice.addItems(installed_voices())
        index = self._voice.findText(self._config.tts.voice)
        if index >= 0:
            self._voice.setCurrentIndex(index)
        else:
            self._voice.setEditText(self._config.tts.voice)
        self.add_row(
            "Voz",
            self._voice,
            "Se listan las voces SAPI del sistema. Se añaden desde Configuración → Hora e "
            "idioma → Voz.",
        )

        self._rate = QSlider(Qt.Orientation.Horizontal)
        self._rate.setRange(-10, 10)
        self._rate.setValue(self._config.tts.rate)
        self._rate.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._rate.setTickInterval(5)
        self._rate.setToolTip("De -10 (muy lento) a +10 (muy rápido). 0 es lo normal.")
        self._rate_value = QLabel(str(self._config.tts.rate))
        self._rate_value.setMinimumWidth(28)
        self._rate.valueChanged.connect(lambda value: self._rate_value.setText(str(value)))
        self.add_row("Velocidad", self._slider_row(self._rate, self._rate_value))

        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._volume.setRange(0, 100)
        self._volume.setValue(self._config.tts.volume)
        self._volume.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._volume.setTickInterval(25)
        self._volume.setToolTip("Volumen del sintetizador, no del mezclador de Windows.")
        self._volume_value = QLabel(f"{self._config.tts.volume} %")
        self._volume_value.setMinimumWidth(42)
        self._volume.valueChanged.connect(lambda value: self._volume_value.setText(f"{value} %"))
        self.add_row("Volumen", self._slider_row(self._volume, self._volume_value))

    def _build_table(self) -> None:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        caption = QLabel("Mensajes")
        caption.setFont(theme.font(10, bold=True))
        column.addWidget(caption)

        self._table = QTableWidget(len(VOICE_KEYS), len(COLUMNS))
        self._table.setHorizontalHeaderLabels(list(COLUMNS))
        vertical = self._table.verticalHeader()
        if vertical is not None:
            vertical.setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.SelectedClicked
            | QTableWidget.EditTrigger.EditKeyPressed
        )
        header = self._table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(3, 120)
        self._table.setToolTip(
            "Doble clic en una celda para editarla. Las casillas y los números se cambian "
            "directamente."
        )

        for row, key in enumerate(VOICE_KEYS):
            self._fill_row(row, key)

        self._table.currentCellChanged.connect(lambda *_args: self._refresh_preview())
        self._table.itemChanged.connect(lambda _item: self._refresh_preview())
        column.addWidget(self._table)
        self._table.setMinimumHeight(210)
        self.add_full_row(holder)

    def _fill_row(self, row: int, key: str) -> None:
        event = self._config.tts.event(key)

        title = QTableWidgetItem(EVENT_TITLES.get(key, key))
        title.setIcon(icons.icon(EVENT_ICONS.get(key, "mic"), theme.MUTED, 18))
        title.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        title.setToolTip(f"{EVENT_TITLES.get(key, key)}: {EVENT_HINTS.get(key, '')}")
        self._table.setItem(row, 0, title)

        enabled = QCheckBox()
        enabled.setChecked(event.enabled)
        enabled.setToolTip("Desmarcado, este aviso no se pronuncia aunque ocurra.")
        enabled.toggled.connect(self._refresh_preview)
        self._table.setCellWidget(row, 1, self._centered(enabled))

        text = QTableWidgetItem(event.text)
        text.setToolTip("Frase hablada. Admite {hora}, {evento}, {zona}, {duracion}, {n} y {max}.")
        self._table.setItem(row, 2, text)

        label = QTableWidgetItem(event.label)
        label.setToolTip(
            "Texto grande de estado en la ventana principal. Solo se usa en armada, "
            "desarmada y disparada."
        )
        if key not in LABEL_KEYS:
            label.setFlags(Qt.ItemFlag.ItemIsSelectable)
            label.setToolTip("Este aviso no cambia la etiqueta de estado de la ventana.")
        self._table.setItem(row, 3, label)

        repeat = QSpinBox()
        repeat.setRange(0, 10)
        repeat.setValue(event.repeat)
        repeat.setSuffix(" x")
        repeat.setToolTip("Cuántas veces seguidas se dice. 0 = no se pronuncia.")
        repeat.valueChanged.connect(self._refresh_preview)
        self._table.setCellWidget(row, 4, self._centered(repeat))

        delay = self._seconds(event.delay_ms, 120.0)
        delay.setToolTip("Espera antes de empezar a hablar. Útil para no pisar la sirena.")
        delay.valueChanged.connect(self._refresh_preview)
        self._table.setCellWidget(row, 5, delay)

        gap = self._seconds(event.gap_ms, 30.0)
        gap.setToolTip("Pausa entre una repetición y la siguiente.")
        gap.valueChanged.connect(self._refresh_preview)
        self._table.setCellWidget(row, 6, gap)

        log = QCheckBox()
        log.setChecked(event.log)
        log.setToolTip("Deja una fila «Voz» en el registro cada vez que se pronuncia.")
        log.toggled.connect(self._refresh_preview)
        self._table.setCellWidget(row, 7, self._centered(log))

        self._rows[key] = _Row(
            enabled=enabled,
            text=text,
            label=label,
            repeat=repeat,
            delay=delay,
            gap=gap,
            log=log,
        )

    @staticmethod
    def _centered(widget: QWidget) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(widget)
        return holder

    @staticmethod
    def _seconds(value_ms: int, maximum: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, maximum)
        spin.setDecimals(1)
        spin.setSingleStep(0.5)
        spin.setSuffix(" s")
        spin.setValue(value_ms / 1000.0)
        return spin

    @staticmethod
    def _slider_row(slider: QSlider, value: QLabel) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(slider, stretch=1)
        row.addWidget(value)
        return holder

    # -- Vista -------------------------------------------------------------

    def _selected_key(self) -> str:
        row = self._table.currentRow()
        if 0 <= row < len(VOICE_KEYS):
            return VOICE_KEYS[row]
        return VOICE_KEYS[0]

    def _resolved(self, key: str) -> str:
        template = self._rows[key].text.text()
        values = {
            "hora": time.strftime("%H:%M"),
            "evento": EVENT_TITLES.get(key, key).lower(),
            "zona": "salón",
            "duracion": "0:00",
            # Valores de muestra para la fila del rearme: sin ellos la vista
            # previa mostraria la plantilla con las llaves puestas.
            "n": "1",
            "max": "3",
        }
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            return template

    def _refresh_preview(self, *_args: object) -> None:
        if self._updating:
            return
        key = self._selected_key()
        row = self._rows.get(key)
        if row is None:
            return
        spoken = self._resolved(key)
        repeats = row.repeat.value()
        gap = row.gap.value()
        delay = row.delay.value()

        if not row.enabled.isChecked():
            detail = "desactivado: no se pronuncia"
        elif repeats <= 0:
            detail = "con 0 repeticiones no se pronuncia"
        else:
            pieces = [f"{repeats} {'repetición' if repeats == 1 else 'repeticiones'}"]
            if repeats > 1:
                pieces.append(f"cada {gap:.1f} s")
            pieces.append(f"empieza a los {delay:.1f} s")
            pieces.append("queda en el registro" if row.log.isChecked() else "no va al registro")
            detail = " · ".join(pieces)

        label_text = row.label.text().strip() or "—"
        self._preview.setText(
            f"<b>Se dirá:</b> «{spoken or '(sin texto)'}»<br>{detail} · etiqueta: {label_text}"
        )

    # -- Datos -------------------------------------------------------------

    def build_event(self, key: str) -> VoiceEventConfig:
        row = self._rows[key]
        return VoiceEventConfig(
            enabled=row.enabled.isChecked(),
            text=row.text.text().strip(),
            label=row.label.text().strip(),
            repeat=row.repeat.value(),
            gap_ms=int(round(row.gap.value() * 1000)),
            delay_ms=int(round(row.delay.value() * 1000)),
            log=row.log.isChecked(),
        )

    def build_config(self) -> AppConfig:
        """Configuracion completa: los seis eventos, los ajustes de voz y las etiquetas."""
        events = {key: self.build_event(key) for key in VOICE_KEYS}
        labels = dict(self._config.ui.labels)
        for key in LABEL_KEYS:
            value = events[key].label
            if value:
                labels[key] = value

        tts = replace(
            self._config.tts,
            enabled=self._enabled.isChecked(),
            voice=self._voice.currentText().strip(),
            rate=self._rate.value(),
            volume=self._volume.value(),
            events=events,
        )
        return replace(self._config, tts=tts, ui=replace(self._config.ui, labels=labels))

    # -- Acciones ----------------------------------------------------------

    def _try(self) -> None:
        if self._on_try is None:
            return
        key = self._selected_key()
        row = self._rows[key]
        text = self._resolved(key)
        if not text:
            QMessageBox.information(self, "Nada que probar", "Escribe primero el texto a decir.")
            return
        self._on_try(
            text,
            row.repeat.value(),
            int(round(row.delay.value() * 1000)),
            int(round(row.gap.value() * 1000)),
        )

    def _reset(self) -> None:
        self._updating = True
        try:
            for key in VOICE_KEYS:
                row = self._rows[key]
                row.enabled.setChecked(True)
                row.text.setText(DEFAULT_MESSAGES.get(key, ""))
                row.label.setText(DEFAULT_LABELS.get(key, key.upper()))
                row.repeat.setValue(2)
                row.delay.setValue(0.0)
                row.gap.setValue(0.0)
                row.log.setChecked(True)
            self._enabled.setChecked(True)
            self._rate.setValue(0)
            self._volume.setValue(90)
        finally:
            self._updating = False
        self._refresh_preview()

    def accept(self) -> None:
        missing = [
            EVENT_TITLES.get(key, key)
            for key in VOICE_KEYS
            if self._rows[key].enabled.isChecked() and not self._rows[key].text.text().strip()
        ]
        if missing:
            QMessageBox.warning(
                self,
                "Texto vacío",
                "Estos avisos están activos pero no tienen texto:\n"
                + "\n".join(f"· {name}" for name in missing)
                + "\n\nEscribe una frase o desmárcalos.",
            )
            return
        try:
            self._on_save(self.build_config())
        except Exception as exc:  # noqa: BLE001 - el fallo se muestra, no se propaga
            QMessageBox.critical(
                self, "No se pudo guardar", f"La configuración no se ha guardado:\n{exc}"
            )
            return
        super().accept()
