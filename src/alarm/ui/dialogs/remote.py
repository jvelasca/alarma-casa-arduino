"""Dialogo del mando remoto por ntfy.

Configura y vigila el canal por el que el PC escucha ordenes del movil. La mitad
de arriba es configuracion (tema, clave, TOTP, que ordenes se aceptan) y la de
abajo es estado en vivo: si esta escuchando, la ultima orden y las ultimas 20 de
la sesion.

El dialogo no habla con ntfy. Pide una `snapshot()` al listener de la ventana y
le entrega el boton de «Enviar estado al movil»: asi no hay dos hilos tocando la
red ni dos sitios que sepan el estado de la alarma.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from secrets import token_urlsafe

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...config import AppConfig, valid_ntfy_topic
from ...services.ntfy_commands import RemoteState
from ...services.totp import generate_secret, normalise_secret, provisioning_uri
from .. import icons, theme
from .common import Dialog

#: Longitud minima de la clave. Doce caracteres de `token_urlsafe` son ~72 bits.
MIN_KEY_CHARS = 12
#: Con menos de esto, un codigo de seis cifras se puede probar por fuerza bruta.
MIN_TOTP_BYTES = 10


class RemoteDialog(Dialog):
    """Configuracion y estado del mando remoto."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        config: AppConfig,
        on_save: Callable[[AppConfig, dict[str, str]], None],
        snapshot: Callable[[], RemoteState],
        on_send_status: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            parent,
            title="Mando remoto por ntfy",
            subtitle=(
                "El PC escucha órdenes publicadas desde la app ntfy del móvil y responde "
                "en el tema de avisos. Las órdenes van por un tema aparte."
            ),
            icon_name="key",
        )
        self._config = config
        self._on_save = on_save
        self._snapshot = snapshot
        self._on_send_status = on_send_status
        #: True mientras se escriben valores derivados (generar secreto, etc.).
        self._filling = False

        self.setMinimumSize(720, 560)
        self.resize(840, 740)
        self.add_full_row(self._scrollable(self._build_page()))
        self.add_standard_buttons()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_state)
        self._timer.start()
        self._refresh_status()
        self._refresh_state()

    # ------------------------------------------------------------------
    # Construccion
    # ------------------------------------------------------------------

    def _build_page(self) -> QWidget:
        cfg = self._config.remote
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 12, 4, 8)
        layout.setSpacing(12)

        intro = QFormLayout()
        intro.setHorizontalSpacing(16)
        intro.setVerticalSpacing(8)
        intro.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(intro)

        self._enabled = QCheckBox("Aceptar órdenes por ntfy")
        self._enabled.setChecked(cfg.enabled)
        self._enabled.setToolTip(
            "Escucha el tema de mando mientras la aplicación esté abierta. Con el PC "
            "apagado la orden no se ejecuta."
        )
        self._enabled.toggled.connect(self._refresh_status)
        intro.addRow(self._enabled)

        self._ntfy_info = QLabel("")
        self._ntfy_info.setObjectName("muted")
        self._ntfy_info.setFont(theme.font(8))
        self._ntfy_info.setWordWrap(True)
        intro.addRow(self._ntfy_info)

        self._server = QLineEdit(self._config.ntfy.base_url)
        self._server.setReadOnly(True)
        self._server.setToolTip("Se cambia en Avisos → Correo, SMS y móvil…")
        intro.addRow("Servidor", self._server)

        self._alert_topic = QLineEdit(self._config.ntfy.topic or "(sin tema de avisos)")
        self._alert_topic.setReadOnly(True)
        self._alert_topic.setToolTip(
            "Tema donde llegan los avisos y donde se responden las órdenes."
        )
        intro.addRow("Tema de avisos", self._alert_topic)

        topic_row = QWidget()
        topic_layout = QHBoxLayout(topic_row)
        topic_layout.setContentsMargins(0, 0, 0, 0)
        topic_layout.setSpacing(8)
        self._topic = QLineEdit(cfg.topic)
        self._topic.setPlaceholderText(cfg.topic_for(self._config.ntfy) or "<tema de avisos>-mando")
        self._topic.setToolTip(
            "Tema donde se publican las órdenes. Vacío: se usa el tema de avisos con el "
            "sufijo «-mando». Debe caber en 64 caracteres."
        )
        self._topic.textChanged.connect(self._refresh_status)
        copy_topic = QPushButton("Copiar")
        copy_topic.setIcon(icons.icon("copy", theme.ACCENT, 18))
        copy_topic.setToolTip("Copia el tema de mando efectivo, para pegarlo en el móvil.")
        copy_topic.clicked.connect(self._copy_topic)
        topic_layout.addWidget(self._topic, 1)
        topic_layout.addWidget(copy_topic)
        self._topic_hint = QLabel("")
        self._topic_hint.setObjectName("muted")
        self._topic_hint.setFont(theme.font(8))
        self._topic_hint.setWordWrap(True)
        intro.addRow("Tema de mando", topic_row)
        intro.addRow("", self._topic_hint)

        # -- Clave y TOTP ------------------------------------------------

        layout.addWidget(self._separator("Credencial de las órdenes"))

        creds = QFormLayout()
        creds.setHorizontalSpacing(16)
        creds.setVerticalSpacing(8)
        creds.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(creds)

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(8)
        self._key = QLineEdit("")
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("sin cambios" if cfg.key else "mínimo 12 caracteres")
        self._key.setToolTip(
            "Se escribe en la orden: «clave armar». Se guarda en .env, nunca en el TOML."
        )
        self._key.textChanged.connect(self._refresh_status)
        show_key = QCheckBox("Mostrar")
        show_key.setToolTip("Muestra la clave que estás escribiendo.")
        show_key.toggled.connect(
            lambda shown: self._key.setEchoMode(
                QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
            )
        )
        generate_key = QPushButton("Generar clave")
        generate_key.setIcon(icons.icon("refresh", theme.ACCENT, 18))
        generate_key.setToolTip("Rellena una clave larga y aleatoria.")
        generate_key.clicked.connect(self._generate_key)
        key_layout.addWidget(self._key, 1)
        key_layout.addWidget(show_key)
        key_layout.addWidget(generate_key)
        creds.addRow("Clave de mando", key_row)

        self._totp_enabled = QCheckBox("Usar código TOTP de una app de autenticación")
        self._totp_enabled.setChecked(bool(cfg.totp_secret))
        self._totp_enabled.setToolTip(
            "Con TOTP activo la clave deja de aceptarse: el código de seis cifras no viaja "
            "en el mensaje, así que quien lea el tema no puede reutilizarlo."
        )
        self._totp_enabled.toggled.connect(self._on_toggle_totp)
        creds.addRow(self._totp_enabled)

        self._totp_secret = QLineEdit("")
        self._totp_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._totp_secret.setPlaceholderText(
            "sin cambios" if cfg.totp_secret else "genera un secreto o pega el tuyo"
        )
        self._totp_secret.textChanged.connect(self._refresh_status)
        totp_row = QWidget()
        totp_layout = QHBoxLayout(totp_row)
        totp_layout.setContentsMargins(0, 0, 0, 0)
        totp_layout.setSpacing(8)
        self._totp_generate = QPushButton("Generar secreto")
        self._totp_generate.setIcon(icons.icon("refresh", theme.ACCENT, 18))
        self._totp_generate.clicked.connect(self._generate_totp)
        self._totp_copy = QPushButton("Copiar URI")
        self._totp_copy.setIcon(icons.icon("copy", theme.ACCENT, 18))
        self._totp_copy.setToolTip(
            "Copia la URI otpauth:// que se pega en Google Authenticator, Authy o similar."
        )
        self._totp_copy.clicked.connect(self._copy_totp_uri)
        totp_layout.addWidget(self._totp_secret, 1)
        totp_layout.addWidget(self._totp_generate)
        totp_layout.addWidget(self._totp_copy)
        creds.addRow("Secreto TOTP", totp_row)

        self._totp_note = QLabel("")
        self._totp_note.setObjectName("muted")
        self._totp_note.setFont(theme.font(8))
        self._totp_note.setWordWrap(True)
        creds.addRow("", self._totp_note)

        self._public_note = QLabel("")
        self._public_note.setFont(theme.font(8))
        self._public_note.setWordWrap(True)
        self._public_note.setStyleSheet(f"color: {theme.WARN};")
        self._public_note.hide()
        creds.addRow("", self._public_note)

        # -- Ordenes permitidas ------------------------------------------

        layout.addWidget(self._separator("Órdenes que se aceptan"))

        self._allow_arm = QCheckBox("Armar")
        self._allow_arm.setChecked(cfg.allow_arm)
        self._allow_disarm = QCheckBox("Desarmar")
        self._allow_disarm.setChecked(cfg.allow_disarm)
        self._allow_status = QCheckBox("Consultar")
        self._allow_status.setChecked(cfg.allow_status)
        self._allow_silence = QCheckBox("Silenciar")
        self._allow_silence.setChecked(cfg.allow_silence)
        self._allow_panic = QCheckBox("Pánico")
        self._allow_panic.setChecked(cfg.allow_panic)
        self._allow_panic.setToolTip(
            "Hace sonar la sirena al instante. Es fácil pulsarlo sin querer desde el "
            "bolsillo, así que viene desactivado."
        )
        orders_row = QWidget()
        orders_layout = QHBoxLayout(orders_row)
        orders_layout.setContentsMargins(0, 0, 0, 0)
        orders_layout.setSpacing(12)
        for box in (
            self._allow_arm,
            self._allow_disarm,
            self._allow_status,
            self._allow_silence,
            self._allow_panic,
        ):
            orders_layout.addWidget(box)
        orders_layout.addStretch(1)
        layout.addWidget(orders_row)

        self._panic_note = QLabel(
            "Pánico dispara la sirena sin esperar al sensor: úsalo solo si sabes que el "
            "móvil no va a enviarlo por accidente."
        )
        self._panic_note.setObjectName("muted")
        self._panic_note.setFont(theme.font(8))
        self._panic_note.setWordWrap(True)
        layout.addWidget(self._panic_note)

        # -- Respuestas y bloqueo ----------------------------------------

        layout.addWidget(self._separator("Respuestas y seguridad"))

        replies = QFormLayout()
        replies.setHorizontalSpacing(16)
        replies.setVerticalSpacing(8)
        replies.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(replies)

        self._reply = QCheckBox("Responder al móvil con el resultado de cada orden")
        self._reply.setChecked(cfg.reply)
        self._reply.setToolTip("La respuesta llega al tema de avisos, con el prefijo [Mando].")
        replies.addRow(self._reply)

        lock_row = QWidget()
        lock_layout = QHBoxLayout(lock_row)
        lock_layout.setContentsMargins(0, 0, 0, 0)
        lock_layout.setSpacing(8)
        self._lockout_failures = QSpinBox()
        self._lockout_failures.setRange(0, 50)
        self._lockout_failures.setValue(cfg.lockout_failures)
        self._lockout_failures.setToolTip(
            "0 desactiva el bloqueo. Cada fallo cuenta, venga de donde venga."
        )
        during = QLabel("durante")
        during.setObjectName("muted")
        self._lockout_minutes = QSpinBox()
        self._lockout_minutes.setRange(1, 1440)
        self._lockout_minutes.setValue(cfg.lockout_minutes)
        self._lockout_minutes.setSuffix(" min")
        self._lockout_minutes.setToolTip(
            "Tiempo que se ignoran las órdenes tras agotar los intentos."
        )
        lock_layout.addWidget(QLabel("Bloquear tras"))
        lock_layout.addWidget(self._lockout_failures)
        lock_layout.addWidget(QLabel("fallos"))
        lock_layout.addWidget(during)
        lock_layout.addWidget(self._lockout_minutes)
        lock_layout.addStretch(1)
        replies.addRow("Intentos", lock_row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self._send_status = QPushButton("Enviar estado al móvil")
        self._send_status.setIcon(icons.icon("phone", theme.ACCENT, 18))
        self._send_status.setToolTip(
            "Publica ahora una respuesta de estado: es la prueba del canal."
        )
        self._send_status.clicked.connect(self._send_state)
        self._send_status.setEnabled(self._on_send_status is not None)
        open_docs = QPushButton("Abrir guía de ntfy")
        open_docs.setIcon(icons.icon("info", theme.ACCENT, 18))
        open_docs.setToolTip("Cómo instalar la app y suscribirse a un tema.")
        open_docs.clicked.connect(self._open_docs)
        actions.addWidget(self._send_status)
        actions.addWidget(open_docs)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._status = QLabel("")
        self._status.setObjectName("muted")
        self._status.setFont(theme.font(8))
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        # -- Estado en vivo ----------------------------------------------

        layout.addWidget(self._separator("Estado en vivo"))

        self._live = QLabel("Desactivado.")
        self._live.setObjectName("muted")
        self._live.setFont(theme.font(9))
        self._live.setWordWrap(True)
        layout.addWidget(self._live)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Hora", "Orden", "Resultado"])
        side = self._table.verticalHeader()
        if side is not None:
            side.setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setMinimumHeight(150)
        header = self._table.horizontalHeader()
        if header is not None:
            header.setStretchLastSection(True)
        self._table.setColumnWidth(0, 80)
        self._table.setColumnWidth(1, 260)
        layout.addWidget(self._table)

        layout.addStretch(1)
        self._on_toggle_totp(self._totp_enabled.isChecked())
        return page

    # ------------------------------------------------------------------
    # Construccion: ayudas
    # ------------------------------------------------------------------

    def _separator(self, title: str) -> QWidget:
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(10)
        label = QLabel(title)
        label.setFont(theme.font(9, bold=True))
        line = QWidget()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.LINE};")
        layout.addWidget(label)
        layout.addWidget(line, 1)
        return holder

    @staticmethod
    def _scrollable(page: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(page)
        return area

    # ------------------------------------------------------------------
    # Estado de la configuracion
    # ------------------------------------------------------------------

    def _ntfy_ready(self) -> bool:
        return bool(self._config.ntfy.enabled and self._config.ntfy.configured)

    def _effective_topic(self) -> str:
        explicit = self._topic.text().strip()
        if explicit:
            return explicit
        return self._config.remote.topic_for(self._config.ntfy)

    def _current_secret(self) -> str:
        return self._totp_secret.text().strip() or self._config.remote.totp_secret

    def _current_key(self) -> str:
        return self._key.text() or self._config.remote.key

    def _refresh_status(self) -> None:
        """Dice si con lo escrito se puede escuchar y que falta si no."""
        config = self.build_config()
        topic = config.remote_topic
        if self._topic.text().strip():
            self._topic_hint.setText(f"Las órdenes se publican en «{topic}».")
        else:
            self._topic_hint.setText(
                f"Derivado del tema de avisos: «{topic or '(sin tema)'}». "
                "Debe caber en 64 caracteres."
            )

        if not self._ntfy_ready():
            self._ntfy_info.setText(
                "El aviso al móvil está apagado o incompleto: el mando no puede escuchar. "
                "Se activa en Avisos → Correo, SMS y móvil…"
            )
            self._ntfy_info.setStyleSheet(f"color: {theme.WARN};")
        else:
            self._ntfy_info.setText(
                "El mando usa el servidor y el token del aviso al móvil "
                f"({self._config.ntfy.base_url})."
            )
            self._ntfy_info.setStyleSheet(f"color: {theme.MUTED};")

        self._totp_note.setText(
            "Con TOTP activo, la clave de mando deja de aceptarse: el código cambia cada "
            "30 s y no viaja en el mensaje. Mantén el reloj del PC en hora."
        )
        # En ntfy.sh cualquiera que lea el tema ve la clave; el codigo TOTP caduca.
        if (
            self._config.ntfy.uses_public_server
            and not config.remote.uses_totp
            and self._current_key()
        ):
            self._public_note.setText(
                "En ntfy.sh el tema no es privado: quien lo lea verá la clave de mando. "
                "Activa TOTP o usa un servidor propio con token."
            )
            self._public_note.show()
        else:
            self._public_note.hide()
        for widget in (self._totp_secret, self._totp_generate, self._totp_copy):
            widget.setEnabled(self._totp_enabled.isChecked())

        problems: list[str] = []
        if not self._ntfy_ready():
            problems.append("activa el aviso al móvil")
        if not topic:
            problems.append("falta el tema de mando")
        elif not valid_ntfy_topic(topic):
            problems.append("el tema de mando no es válido")
        elif len(topic) > 64:
            problems.append("el tema de mando pasa de 64 caracteres")
        if not config.remote.authenticates:
            problems.append("falta la clave o el código TOTP")
        elif config.remote.uses_totp and len(normalise_secret(config.remote.totp_secret)) < 16:
            problems.append("el secreto TOTP es demasiado corto")

        if problems:
            self._status.setText("Para escuchar falta: " + ", ".join(problems) + ".")
            self._status.setStyleSheet(f"color: {theme.WARN};")
            return
        if self._enabled.isChecked():
            self._status.setText("Listo: el PC escuchará las órdenes en cuanto guardes.")
            self._status.setStyleSheet(f"color: {theme.OK};")
            return
        self._status.setText("Todo listo; marca «Aceptar órdenes por ntfy» para escuchar.")
        self._status.setStyleSheet(f"color: {theme.MUTED};")

    def _refresh_state(self) -> None:
        """Pinta la fotografia del listener una vez por segundo."""
        state = self._snapshot()
        if not self._enabled.isChecked():
            self._live.setText("Desactivado: no se escucha ningún tema.")
            self._live.setStyleSheet(f"color: {theme.MUTED};")
        elif state.listening and state.connected:
            since = time.strftime("%H:%M:%S", time.localtime(state.since))
            self._live.setText(
                f"Escuchando «{state.topic}» desde las {since}. "
                f"Recibidas {state.received}, aceptadas {state.accepted}, "
                f"rechazadas {state.rejected}."
            )
            self._live.setStyleSheet(f"color: {theme.OK};")
        elif state.listening:
            self._live.setText(
                f"Sin conexión con «{state.topic}». Se reintenta solo."
                + (f"\nÚltimo error: {state.error}" if state.error else "")
            )
            self._live.setStyleSheet(f"color: {theme.WARN};")
        else:
            self._live.setText(
                f"Sin datos del mando: {state.error or 'el canal no está escuchando'}."
            )
            self._live.setStyleSheet(f"color: {theme.WARN};")

        if state.last_at:
            hour = time.strftime("%H:%M:%S", time.localtime(state.last_at))
            self._live.setText(
                self._live.text()
                + f"\nÚltima orden: {state.last_text} a las {hour} → {state.last_result}"
            )

        self._table.setRowCount(0)
        for entry in reversed(state.entries):
            row = self._table.rowCount()
            self._table.insertRow(row)
            hour = time.strftime("%H:%M:%S", time.localtime(entry.at))
            self._table.setItem(row, 0, QTableWidgetItem(hour))
            self._table.setItem(row, 1, QTableWidgetItem(entry.text))
            result = QTableWidgetItem(entry.result)
            result.setForeground(QColor(theme.OK if entry.ok else theme.FAIL))
            self._table.setItem(row, 2, result)

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------

    def _generate_key(self) -> None:
        self._key.setText(token_urlsafe(16))
        self._key.setEchoMode(QLineEdit.EchoMode.Normal)
        self._key.setFocus()
        self._refresh_status()

    def _on_toggle_totp(self, enabled: bool) -> None:
        for widget in (self._totp_secret, self._totp_generate, self._totp_copy):
            widget.setEnabled(enabled)
        if enabled and not self._current_secret():
            self._generate_totp()
        self._refresh_status()

    def _generate_totp(self) -> None:
        self._totp_secret.setText(generate_secret())
        self._totp_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self._refresh_status()

    def _copy_topic(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._effective_topic())

    def _copy_totp_uri(self) -> None:
        secret = self._current_secret()
        if not secret:
            self._generate_totp()
            secret = self._current_secret()
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(provisioning_uri(secret, label="alarma-casa"))

    def _send_state(self) -> None:
        if self._on_send_status is not None:
            self._on_send_status()

    def _open_docs(self) -> None:
        QDesktopServices.openUrl(QUrl("https://docs.ntfy.sh/subscribe/phone/"))

    # ------------------------------------------------------------------
    # Contrato con la ventana
    # ------------------------------------------------------------------

    def build_config(self) -> AppConfig:
        totp_enabled = self._totp_enabled.isChecked()
        remote = replace(
            self._config.remote,
            enabled=self._enabled.isChecked(),
            topic=self._topic.text().strip(),
            allow_arm=self._allow_arm.isChecked(),
            allow_disarm=self._allow_disarm.isChecked(),
            allow_status=self._allow_status.isChecked(),
            allow_silence=self._allow_silence.isChecked(),
            allow_panic=self._allow_panic.isChecked(),
            reply=self._reply.isChecked(),
            lockout_failures=self._lockout_failures.value(),
            lockout_minutes=self._lockout_minutes.value(),
            key=self._current_key(),
            totp_secret=self._current_secret() if totp_enabled else "",
        )
        return replace(self._config, remote=remote)

    def secrets(self) -> dict[str, str]:
        """Solo lo que ha cambiado; vacio no toca `.env`."""
        values: dict[str, str] = {}
        key = self._key.text()
        if key:
            values["ALARM_REMOTE_KEY"] = key
        if not self._totp_enabled.isChecked() and self._config.remote.totp_secret:
            # Apagar TOTP tiene que borrar el secreto del `.env`; si no, al
            # recargar volveria a estar activo y la clave no valdria.
            values["ALARM_REMOTE_TOTP"] = ""
        elif self._totp_secret.text():
            values["ALARM_REMOTE_TOTP"] = self._totp_secret.text().strip()
        return values

    def _validate(self) -> str:
        config = self.build_config()
        if not config.remote.enabled:
            return ""
        if not self._ntfy_ready():
            return (
                "Para aceptar órdenes hace falta el aviso al móvil: activa ntfy y rellena "
                "servidor y tema en Avisos → Correo, SMS y móvil…"
            )
        topic = config.remote_topic
        if not valid_ntfy_topic(topic):
            return "El tema de mando solo admite letras, números, guion y guion bajo (1 a 64)."
        if len(topic) > 64:
            return "El tema de mando pasa de 64 caracteres: escribe uno más corto."
        if not config.remote.authenticates:
            return "Escribe una clave de mando o genera un secreto TOTP."
        key = self._key.text()
        if key and len(key) < MIN_KEY_CHARS:
            return f"La clave de mando debe tener al menos {MIN_KEY_CHARS} caracteres."
        if config.remote.uses_totp:
            secret = normalise_secret(config.remote.totp_secret)
            if len(secret) < MIN_TOTP_BYTES * 2:
                return "El secreto TOTP es demasiado corto: genera uno nuevo."
        return ""

    def accept(self) -> None:
        problem = self._validate()
        if problem:
            QMessageBox.warning(self, "Mando remoto", problem)
            return
        try:
            self._on_save(self.build_config(), self.secrets())
        except Exception as exc:  # noqa: BLE001 - se muestra al usuario
            QMessageBox.critical(
                self, "No se pudo guardar", f"La configuración no se ha guardado:\n{exc}"
            )
            return
        self._timer.stop()
        super().accept()

    def reject(self) -> None:
        self._timer.stop()
        super().reject()


__all__ = ["MIN_KEY_CHARS", "RemoteDialog"]
