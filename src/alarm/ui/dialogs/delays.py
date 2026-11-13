"""Ventana de retardos y rearme automatico.

Una alarma que dispara en el mismo instante en que se abre la puerta no se
puede usar: no da tiempo ni a salir de casa ni a llegar al teclado. Las
centrales de mercado separan las dos cosas y las avisan con pitidos, y esto es
lo mismo:

* Retardo de salida: desde que se arma hasta que el sensor vigila. Se usa para
  salir sin que el PIR dispare la alarma.
* Retardo de entrada: desde que el sensor detecta movimiento hasta que suena la
  sirena. Se usa para llegar al teclado y desarmar.
* Rearme automatico: al terminar la sirena, cuanto se espera antes de volver a
  vigilar sola y cuantas veces como maximo.

Los dos retardos son configurables y 0 los desactiva. Las pruebas de esta
ventana usan una cuenta atras corta a proposito: escuchar 45 s de pitidos para
comprobar el zumbador seria una tortura, no una prueba.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from PyQt6.QtWidgets import QCheckBox, QLabel, QMessageBox, QSpinBox, QWidget

from ...config import AppConfig
from .. import theme
from .common import Dialog

#: Duracion de las pruebas. La cadencia se acelera igual que en el uso real,
#: pero en unos segundos: basta para reconocer los dos tonos.
PREVIEW_MS = 8000

#: Rango del rearme automatico. El tope de 120 min evita que un despiste deje
#: la alarma sin vigilar toda la noche; el de 10 rearmes, una sirena en bucle.
MIN_REARM_MIN = 1
MAX_REARM_MIN = 120
MAX_REARMS = 10

_REFUSED = "No se pudo probar: revisa la conexión y que el zumbador esté activado."


class DelaysDialog(Dialog):
    """Ajusta los retardos, el rearme automatico y permite escuchar las cadencias."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        config: AppConfig,
        on_save: Callable[[AppConfig], None],
        on_preview: Callable[[str, int], bool],
        connected: bool = False,
    ) -> None:
        super().__init__(
            parent,
            title="Retardos y rearme",
            subtitle=(
                "Cuánto tarda la alarma en vigilar después de armarla, cuánto espera "
                "antes de dispararse y si vuelve a armarse sola tras un disparo."
            ),
            icon_name="clock",
            icon_color=theme.ACCENT,
        )
        self._config = config
        self._on_save = on_save
        self._on_preview = on_preview
        self._connected = connected

        rules = config.alarm

        self._exit = QSpinBox()
        self._exit.setRange(0, 300)
        self._exit.setSuffix(" s")
        self._exit.setSpecialValueText("Sin retardo")
        self._exit.setValue(rules.exit_delay_s)
        self._exit.setToolTip(
            "Tiempo para salir de casa con la alarma armándose. Durante ese rato el PIR no "
            "dispara y la placa pita cada vez más rápido."
        )
        self.add_row(
            "Retardo de salida",
            self._exit,
            "Desde que pulsas «Armar» hasta que la alarma vigila. Lo normal son 45 s.",
        )

        self._entry = QSpinBox()
        self._entry.setRange(0, 300)
        self._entry.setSuffix(" s")
        self._entry.setSpecialValueText("Sin retardo")
        self._entry.setValue(rules.entry_delay_s)
        self._entry.setToolTip(
            "Tiempo que tienes para desarmar cuando el sensor detecta movimiento. Al terminar, "
            "se dispara la alarma."
        )
        self.add_row(
            "Retardo de entrada",
            self._entry,
            "Desde que el sensor detecta movimiento hasta que suena la sirena. Lo normal son 30 s.",
        )

        note = QLabel(
            "Con un solo PIR, todo el movimiento entra por el retardo de entrada: quien venga "
            "de fuera tendrá el mismo margen que tú para desarmar. Si prefieres disparo "
            "inmediato, pon 0 s y asume que no habrá tiempo para llegar al teclado."
        )
        note.setObjectName("muted")
        note.setFont(theme.font(8))
        note.setWordWrap(True)
        self.add_full_row(note)

        # -- Rearme automatico --------------------------------------------
        self._auto = QCheckBox("Rearmar automáticamente tras un disparo")
        self._auto.setChecked(rules.auto_rearm)
        self._auto.setToolTip(
            "Al terminar la sirena la alarma espera lo indicado y vuelve a vigilar sola, "
            "sin necesidad de armarla a mano."
        )
        self.add_full_row(self._auto)

        self._rearm_min = QSpinBox()
        self._rearm_min.setRange(MIN_REARM_MIN, MAX_REARM_MIN)
        self._rearm_min.setSuffix(" min")
        # El TOML guarda segundos; el dialogo trabaja en minutos porque una
        # espera de rearme se piensa en minutos, no en 300 s.
        self._rearm_min.setValue(max(MIN_REARM_MIN, round(rules.rearm_delay_s / 60)))
        self._rearm_min.setToolTip(
            "Cuenta desde que termina la sirena. Solo se aplica si el rearme está marcado."
        )
        self.add_row(
            "Espera antes de rearmar",
            self._rearm_min,
            "Tiempo de silencio tras la sirena antes de volver a vigilar. Lo normal son 5 min.",
        )

        self._rearm_max = QSpinBox()
        self._rearm_max.setRange(1, MAX_REARMS)
        self._rearm_max.setValue(rules.max_rearms)
        self._rearm_max.setToolTip(
            "Cuántas veces seguidas puede rearmarse sola. Al agotarlas queda DISPARADA "
            "hasta que la desarmes a mano."
        )
        self.add_row(
            "Rearmes máximos",
            self._rearm_max,
            "Tope de rearmes seguidos. Existe para que un sensor defectuoso no deje la "
            "sirena sonando en bucle toda la tarde.",
        )

        rearm_note = QLabel(
            "El rearme va directo a ARMADA, sin retardo de salida: quien ha provocado el "
            "disparo ya está dentro. Desarmar a mano deja el contador a cero."
        )
        rearm_note.setObjectName("muted")
        rearm_note.setFont(theme.font(8))
        rearm_note.setWordWrap(True)
        self.add_full_row(rearm_note)

        self._auto.toggled.connect(self._sync_enabled)
        self._sync_enabled(rules.auto_rearm)

        self._feedback = QLabel()
        self._feedback.setObjectName("muted")
        self._feedback.setFont(theme.font(8))
        self._feedback.setWordWrap(True)
        self.add_full_row(self._feedback)

        self._exit_button = self.add_left_button(
            "Escuchar salida",
            lambda: self._preview("exit"),
            "Pitidos lentos que se aceleran, como cuando sales de casa.",
        )
        self._entry_button = self.add_left_button(
            "Escuchar entrada",
            lambda: self._preview("entry"),
            "Pitidos más agudos y rápidos, como cuando llegas y tienes que desarmar.",
        )
        self.add_standard_buttons()

        if not connected:
            self._exit_button.setEnabled(False)
            self._entry_button.setEnabled(False)
            self._feedback.setText(
                "Sin conexión con la placa: los retardos se pueden ajustar, pero no probar."
            )

    # -- Pruebas -----------------------------------------------------------

    def _sync_enabled(self, enabled: bool) -> None:
        """Los dos campos del rearme solo tienen sentido con la casilla marcada."""
        self._rearm_min.setEnabled(enabled)
        self._rearm_max.setEnabled(enabled)

    def _preview(self, kind: str) -> None:
        if not self._connected:
            return
        if self._on_preview(kind, PREVIEW_MS):
            title = "salida" if kind == "exit" else "entrada"
            self._feedback.setText(
                f"Retardo de {title} de {PREVIEW_MS // 1000} s enviado a la placa."
            )
        else:
            self._feedback.setText(_REFUSED)

    # -- Guardado ----------------------------------------------------------

    def build_config(self) -> AppConfig:
        rules = replace(
            self._config.alarm,
            exit_delay_s=self._exit.value(),
            entry_delay_s=self._entry.value(),
            auto_rearm=self._auto.isChecked(),
            rearm_delay_s=self._rearm_min.value() * 60,
            max_rearms=self._rearm_max.value(),
        )
        return replace(self._config, alarm=rules)

    def accept(self) -> None:
        try:
            self._on_save(self.build_config())
        except Exception as exc:  # noqa: BLE001 - el fallo se muestra, no se propaga
            QMessageBox.critical(
                self, "No se pudo guardar", f"La configuración no se ha guardado:\n{exc}"
            )
            return
        super().accept()
