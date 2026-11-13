"""Dialogo de avisos por correo, por SMS y por ntfy.

Tres pestanas, misma idea en todas: activar el canal, elegir a quien avisar, que
eventos lo generan y con que credenciales sale. Las contrasenas y los tokens no
se guardan en el TOML: van a `.env` con escritura atomica.

Un detalle que evita sustos: si el campo de contrasena se deja como esta, no se
toca el valor guardado. Asi se puede cambiar el destinatario sin reescribir la
contrasena de aplicacion, que el usuario probablemente ya no recuerda.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from secrets import token_urlsafe

from PyQt6.QtCore import QSize, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...config import (
    NTFY_PRIORITIES,
    SECURITY_MODES,
    SMS_PROVIDERS,
    AppConfig,
    normalize_ntfy_server,
    valid_ntfy_topic,
)
from ...services.email_sender import SSL_PORT, STARTTLS_PORT, effective_security
from ...services.sms_sender import SMS_MAX_CHARS, clip, normalize_number
from .. import icons, theme
from .common import Dialog, RecipientsEditor, valid_email, valid_phone

TWILIO_CONSOLE = "https://console.twilio.com"
NTFY_DOCS = "https://docs.ntfy.sh/"

PROVIDER_LABELS: dict[str, str] = {
    "twilio": "Twilio (API)",
    "email_gateway": "Pasarela correo → SMS",
}

SECURITY_LABELS: dict[str, str] = {
    "starttls": "STARTTLS (587)",
    "ssl": "SSL (465)",
    "none": "Sin cifrar (no recomendado)",
}

PRIORITY_LABELS: dict[str, str] = {
    "min": "Mínima",
    "low": "Baja",
    "default": "Normal",
    "high": "Alta",
    "urgent": "Urgente",
}

#: Lo que hay que saber de `urgent`, que es la que se elige a conciencia.
PRIORITY_URGENT_HINT = "Urgente suena aunque el móvil esté en silencio; el resto respeta el modo."

#: Pestaña de cada canal, para saltar a la que falla al guardar.
TAB_EMAIL = 0
TAB_SMS = 1
TAB_NTFY = 2


class _ScrolledPage(QScrollArea):
    """Pestaña con desplazamiento cuyo tamaño preferido sigue al formulario.

    `QScrollArea` devuelve un tamaño preferido mínimo, y el diálogo saldría con
    una barra de desplazamiento aunque hubiera sitio de sobra. Aquí el tamaño
    preferido es el del contenido; el diálogo decide luego cuánto de eso cabe
    en la pantalla.
    """

    def sizeHint(self) -> QSize:
        page = self.widget()
        base = super().sizeHint()
        if page is None:
            return base
        content = page.sizeHint()
        return QSize(
            max(base.width(), content.width() + 2),
            max(base.height(), content.height() + 2),
        )


class NotificationsDialog(Dialog):
    """Configuracion de correo, SMS y ntfy con prueba y guardado transaccional."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        config: AppConfig,
        on_save: Callable[[AppConfig, dict[str, str]], None],
        on_test_email: Callable[[AppConfig], None] | None = None,
        on_test_sms: Callable[[AppConfig], None] | None = None,
        on_test_ntfy: Callable[[AppConfig], None] | None = None,
    ) -> None:
        super().__init__(
            parent,
            title="Avisos por correo, SMS y móvil",
            subtitle=(
                "A quién se avisa y con qué credenciales. Las contraseñas y los tokens se "
                "guardan en .env, fuera del repositorio."
            ),
            icon_name="mail",
        )
        self._config = config
        self._on_save = on_save
        self._on_test_email = on_test_email
        self._on_test_sms = on_test_sms
        self._on_test_ntfy = on_test_ntfy
        # Con tres canales, los formularios suman más alto que la pantalla de un
        # portátil. Cada pestaña va dentro de un área de desplazamiento para que
        # el diálogo pueda encogerse sin que la botonera quede bajo el borde.
        self.setMinimumSize(760, 520)
        self.resize(900, 720)
        # Evita que ajustar el puerto y ajustar el cifrado se llamen en bucle.
        self._transport_sync = False

        self._tabs = QTabWidget()
        self._tabs.addTab(self._scrollable(self._build_email_tab()), "Correo")
        self._tabs.addTab(self._scrollable(self._build_sms_tab()), "SMS")
        self._tabs.addTab(self._scrollable(self._build_ntfy_tab()), "Móvil (ntfy)")
        self.add_full_row(self._tabs)
        self._refresh_provider()
        self._refresh_counter()
        self._refresh_sms_status()
        self._refresh_ntfy_status()
        self._fit_initial_size()

        self._status = QLabel("Los cambios se aplican al guardar, sin reiniciar la aplicación.")
        self._status.setObjectName("muted")
        self._status.setFont(theme.font(8))
        self.add_full_row(self._status)

        self.add_standard_buttons()

    # ------------------------------------------------------------------
    # Pestana de correo
    # ------------------------------------------------------------------

    def _build_email_tab(self) -> QWidget:
        cfg = self._config.email
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 12, 4, 8)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        self._email_enabled = QCheckBox("Enviar avisos por correo")
        self._email_enabled.setChecked(cfg.enabled)
        self._email_enabled.setToolTip("Activa o desactiva todo el canal de correo.")
        form.addRow(self._email_enabled)

        self._email_armed, self._email_disarmed, self._email_triggered, self._email_lost = (
            self._events_row(cfg.on_armed, cfg.on_disarmed, cfg.on_triggered, cfg.on_device_lost)
        )
        form.addRow(
            "Avisar cuando",
            self._wrap_row(
                self._email_armed,
                self._email_disarmed,
                self._email_triggered,
                self._email_lost,
            ),
        )

        self._email_host = QLineEdit(cfg.host)
        self._email_host.setPlaceholderText("smtp.gmail.com")
        self._email_host.setToolTip("Servidor SMTP de tu proveedor de correo.")
        form.addRow("Servidor", self._email_host)

        self._email_port = QSpinBox()
        self._email_port.setRange(1, 65535)
        self._email_port.setValue(cfg.port)
        self._email_port.setToolTip("587 con STARTTLS, 465 con SSL.")
        form.addRow("Puerto", self._email_port)

        self._email_security = QComboBox()
        for value in ("starttls", "ssl", "none"):
            if value in SECURITY_MODES:
                self._email_security.addItem(SECURITY_LABELS[value], value)
        index = self._email_security.findData(cfg.security)
        if index >= 0:
            self._email_security.setCurrentIndex(index)
        self._email_security.setToolTip(
            "STARTTLS negocia el cifrado sobre el puerto 587. SSL lo hace desde el principio."
        )
        form.addRow("Seguridad", self._email_security)

        # Puerto y cifrado van juntos: 465 sin SSL (o 587 con SSL) hace que el
        # servidor cierre la conexión nada más saludar. Se sincronizan al vuelo.
        self._email_port.valueChanged.connect(self._on_email_port_changed)
        self._email_security.currentIndexChanged.connect(self._on_email_security_changed)
        self._sync_email_transport_initial(cfg.port, cfg.security)

        self._email_from = QLineEdit(cfg.sender)
        self._email_from.setPlaceholderText("alarma@example.com")
        self._email_from.setToolTip(
            "Remitente que verá quien reciba el aviso. Debe coincidir con la cuenta SMTP."
        )
        form.addRow("Remitente", self._email_from)

        self._email_to = RecipientsEditor(
            kind="email",
            placeholder="destinatario@example.com",
            validator=_looks_like_email,
        )
        self._email_to.set_values(cfg.to)
        form.addRow("Destinatarios", self._email_to)

        self._email_subject = QLineEdit(cfg.subject)
        self._email_subject.setToolTip("Admite {evento} y {hora}.")
        form.addRow("Asunto", self._email_subject)

        self._email_timeout = QDoubleSpinBox()
        self._email_timeout.setRange(1.0, 300.0)
        self._email_timeout.setDecimals(1)
        self._email_timeout.setSuffix(" s")
        self._email_timeout.setValue(cfg.timeout_s)
        self._email_timeout.setToolTip("Sin timeout, un servidor colgado bloquea el aviso.")
        form.addRow("Tiempo máximo", self._email_timeout)

        self._email_retries = QSpinBox()
        self._email_retries.setRange(0, 10)
        self._email_retries.setValue(cfg.retries)
        self._email_retries.setToolTip("Reintentos ante un fallo de red. Cada uno espera más.")
        form.addRow("Reintentos", self._email_retries)

        self._email_user = QLineEdit(cfg.user)
        self._email_user.setPlaceholderText("cuenta@gmail.com")
        self._email_user.setToolTip("Cuenta que se autentica. Se guarda en .env.")
        form.addRow("Usuario", self._email_user)

        self._email_password = QLineEdit("")
        self._email_password.setEchoMode(QLineEdit.EchoMode.Password)
        self._email_password.setPlaceholderText(
            "sin cambios" if cfg.password else "contraseña de aplicación"
        )
        self._email_password.setToolTip(
            "En Gmail es una contraseña de aplicación, no la del correo. Se guarda en .env."
        )
        form.addRow("Contraseña", self._email_password)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self._email_test = self._test_button("Probar correo", self._test_email)
        actions.addWidget(self._email_test)
        layout.addLayout(actions)
        layout.addStretch(1)

        hint = QLabel(
            "Gmail exige verificación en dos pasos y una «contraseña de aplicación». "
            "Otras cuentas admiten su contraseña habitual."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        hint.setFont(theme.font(8))
        layout.addWidget(hint)
        return page

    # ------------------------------------------------------------------
    # Pestana de SMS
    # ------------------------------------------------------------------

    def _build_sms_tab(self) -> QWidget:
        cfg = self._config.sms
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 12, 4, 8)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        self._sms_enabled = QCheckBox("Enviar avisos por SMS")
        self._sms_enabled.setChecked(cfg.enabled)
        self._sms_enabled.setToolTip("Activa o desactiva todo el canal de SMS.")
        form.addRow(self._sms_enabled)

        self._sms_armed, self._sms_disarmed, self._sms_triggered, self._sms_lost = self._events_row(
            cfg.on_armed, cfg.on_disarmed, cfg.on_triggered, cfg.on_device_lost
        )
        form.addRow(
            "Avisar cuando",
            self._wrap_row(
                self._sms_armed,
                self._sms_disarmed,
                self._sms_triggered,
                self._sms_lost,
            ),
        )

        self._sms_provider = QComboBox()
        for value in ("twilio", "email_gateway"):
            if value in SMS_PROVIDERS:
                self._sms_provider.addItem(PROVIDER_LABELS[value], value)
        index = self._sms_provider.findData(cfg.provider)
        if index >= 0:
            self._sms_provider.setCurrentIndex(index)
        self._sms_provider.setToolTip(
            "Twilio es fiable y de pago. La pasarela correo→SMS es gratuita, pero muchos "
            "operadores ya no la ofrecen."
        )
        self._sms_provider.currentIndexChanged.connect(self._refresh_provider)
        form.addRow("Proveedor", self._sms_provider)

        self._sms_to = RecipientsEditor(
            kind="sms",
            placeholder="+34 600 11 22 33",
            validator=valid_phone,
        )
        self._sms_to.set_values(cfg.to)
        form.addRow("Números", self._sms_to)

        self._sms_from = QLineEdit(cfg.from_number)
        self._sms_from.setPlaceholderText("+34600000000")
        self._sms_from.setToolTip("Número comprado o verificado en Twilio.")
        form.addRow("Remitente Twilio", self._sms_from)

        self._sms_gateway = QLineEdit(cfg.gateway_domain)
        self._sms_gateway.setPlaceholderText("txt.movistar.es")
        self._sms_gateway.setToolTip(
            "Solo para la pasarela: el dominio que tu operador usa para correo → SMS."
        )
        form.addRow("Dominio pasarela", self._sms_gateway)

        self._sms_template = QLineEdit(cfg.template)
        self._sms_template.setToolTip(
            f"Texto del SMS. Admite {{evento}} y {{hora}}. Se recorta a {SMS_MAX_CHARS} "
            "caracteres, que es un segmento."
        )
        self._sms_template.textChanged.connect(self._refresh_counter)
        form.addRow("Texto", self._sms_template)

        self._sms_counter = QLabel("")
        self._sms_counter.setObjectName("muted")
        self._sms_counter.setFont(theme.font(8))
        form.addRow("", self._sms_counter)

        self._sms_timeout = QDoubleSpinBox()
        self._sms_timeout.setRange(1.0, 120.0)
        self._sms_timeout.setDecimals(1)
        self._sms_timeout.setSuffix(" s")
        self._sms_timeout.setValue(cfg.timeout_s)
        self._sms_timeout.setToolTip("Tiempo máximo de la petición HTTP.")
        form.addRow("Tiempo máximo", self._sms_timeout)

        self._sms_retries = QSpinBox()
        self._sms_retries.setRange(0, 10)
        self._sms_retries.setValue(cfg.retries)
        self._sms_retries.setToolTip("Reintentos por número si el proveedor falla.")
        form.addRow("Reintentos", self._sms_retries)

        self._sms_sid = QLineEdit(cfg.sid)
        self._sms_sid.setPlaceholderText("ACxxxxxxxx")
        self._sms_sid.setToolTip("Account SID de Twilio. Se guarda en .env.")
        form.addRow("Twilio SID", self._sms_sid)

        self._sms_token = QLineEdit("")
        self._sms_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._sms_token.setPlaceholderText("sin cambios" if cfg.token else "Auth Token")
        self._sms_token.setToolTip("Auth Token de Twilio. Se guarda en .env.")
        form.addRow("Twilio token", self._sms_token)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        open_console = QPushButton("Abrir consola de Twilio")
        open_console.setIcon(icons.icon("info", theme.ACCENT, 18))
        open_console.setToolTip(
            "Abre console.twilio.com, donde están el Account SID, el Auth Token y los "
            "números disponibles."
        )
        open_console.clicked.connect(self._open_twilio)
        actions.addWidget(open_console)
        actions.addStretch(1)
        self._sms_test = self._test_button("Probar SMS", self._test_sms)
        actions.addWidget(self._sms_test)
        layout.addLayout(actions)

        self._sms_status = QLabel("")
        self._sms_status.setObjectName("muted")
        self._sms_status.setFont(theme.font(8))
        self._sms_status.setWordWrap(True)
        layout.addWidget(self._sms_status)

        guide = QLabel(
            "Twilio exige una cuenta propia. El Account SID y el Auth Token están en el "
            "panel de Twilio; el remitente es un número comprado o, en la cuenta de "
            "prueba, uno verificado. Cada SMS a España cuesta unos 0,08 € y la cuenta de "
            "prueba solo entrega a números verificados.\n"
            "La pasarela correo → SMS es gratuita, pero la mayoría de operadores "
            "españoles ya no la ofrece."
        )
        guide.setObjectName("muted")
        guide.setWordWrap(True)
        guide.setFont(theme.font(8))
        layout.addWidget(guide)
        layout.addStretch(1)

        for widget in (self._sms_from, self._sms_gateway, self._sms_sid, self._sms_token):
            widget.textChanged.connect(self._refresh_sms_status)
        self._sms_to.changed.connect(self._refresh_sms_status)
        # Los refrescos iniciales se hacen en `__init__`, cuando ya existen los
        # controles de las tres pestanas: `build_config` los recorre todos.
        return page

    # ------------------------------------------------------------------
    # Pestana del movil (ntfy)
    # ------------------------------------------------------------------

    def _build_ntfy_tab(self) -> QWidget:
        cfg = self._config.ntfy
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 12, 4, 8)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        self._ntfy_enabled = QCheckBox("Enviar avisos al móvil (ntfy)")
        self._ntfy_enabled.setChecked(cfg.enabled)
        self._ntfy_enabled.setToolTip(
            "Notificaciones push gratuitas, sin SMS ni tarjeta SIM. Hace falta la "
            "aplicación ntfy en el teléfono, suscrita al mismo tema."
        )
        form.addRow(self._ntfy_enabled)

        self._ntfy_armed, self._ntfy_disarmed, self._ntfy_triggered, self._ntfy_lost = (
            self._events_row(cfg.on_armed, cfg.on_disarmed, cfg.on_triggered, cfg.on_device_lost)
        )
        form.addRow(
            "Avisar cuando",
            self._wrap_row(
                self._ntfy_armed,
                self._ntfy_disarmed,
                self._ntfy_triggered,
                self._ntfy_lost,
            ),
        )

        self._ntfy_server = QLineEdit(cfg.server)
        self._ntfy_server.setPlaceholderText("https://ntfy.sh")
        self._ntfy_server.setToolTip(
            "Servidor ntfy. El público no requiere cuenta; uno propio permite exigir token."
        )
        form.addRow("Servidor", self._ntfy_server)

        topic_row = QWidget()
        topic_layout = QHBoxLayout(topic_row)
        topic_layout.setContentsMargins(0, 0, 0, 0)
        topic_layout.setSpacing(8)
        self._ntfy_topic = QLineEdit(cfg.topic)
        self._ntfy_topic.setPlaceholderText("alarma-casa-7f92k5x9")
        self._ntfy_topic.setToolTip(
            "El tema es el «canal»: el móvil debe estar suscrito al mismo nombre. "
            "En ntfy.sh cualquiera que lo conozca puede leerlo, así que debe ser largo."
        )
        self._ntfy_generate = QPushButton("Generar tema seguro")
        self._ntfy_generate.setIcon(icons.icon("refresh", theme.ACCENT, 18))
        self._ntfy_generate.setToolTip("Rellena un tema largo y aleatorio, difícil de adivinar.")
        self._ntfy_generate.clicked.connect(self._generate_topic)
        topic_layout.addWidget(self._ntfy_topic, 1)
        topic_layout.addWidget(self._ntfy_generate)
        form.addRow("Tema", topic_row)

        self._ntfy_token = QLineEdit("")
        self._ntfy_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._ntfy_token.setPlaceholderText(
            "sin cambios" if cfg.token else "opcional (solo servidor propio)"
        )
        self._ntfy_token.setToolTip(
            "Token de acceso de tu servidor ntfy. Se guarda en .env. En ntfy.sh se deja vacío."
        )
        form.addRow("Token", self._ntfy_token)

        self._ntfy_title = QLineEdit(cfg.title)
        self._ntfy_title.setToolTip(
            "Título de la notificación. Admite {evento}: se sustituye por DISPARADA, "
            "armada, desarmada o sin sensor."
        )
        form.addRow("Título", self._ntfy_title)

        priorities = self._priorities_row(
            {
                "triggered": cfg.priority_triggered,
                "device_lost": cfg.priority_device_lost,
                "armed": cfg.priority_armed,
                "disarmed": cfg.priority_disarmed,
            },
            form,
        )
        self._ntfy_priority_triggered = priorities["triggered"]
        self._ntfy_priority_lost = priorities["device_lost"]
        self._ntfy_priority_armed = priorities["armed"]
        self._ntfy_priority_disarmed = priorities["disarmed"]

        limits_row = QWidget()
        limits_layout = QHBoxLayout(limits_row)
        limits_layout.setContentsMargins(0, 0, 0, 0)
        limits_layout.setSpacing(8)
        self._ntfy_timeout = QDoubleSpinBox()
        self._ntfy_timeout.setRange(1.0, 120.0)
        self._ntfy_timeout.setDecimals(1)
        self._ntfy_timeout.setSuffix(" s")
        self._ntfy_timeout.setValue(cfg.timeout_s)
        self._ntfy_timeout.setToolTip("Tiempo máximo de la petición HTTPS.")
        retries_caption = QLabel("reintentos")
        retries_caption.setObjectName("muted")
        self._ntfy_retries = QSpinBox()
        self._ntfy_retries.setRange(0, 10)
        self._ntfy_retries.setValue(cfg.retries)
        self._ntfy_retries.setToolTip("Reintentos ante un fallo de red o un 5xx.")
        limits_layout.addWidget(self._ntfy_timeout)
        limits_layout.addWidget(retries_caption)
        limits_layout.addWidget(self._ntfy_retries)
        limits_layout.addStretch(1)
        form.addRow("Tiempo máximo", limits_row)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        open_docs = QPushButton("Abrir guía de ntfy")
        open_docs.setIcon(icons.icon("info", theme.ACCENT, 18))
        open_docs.setToolTip("Abre docs.ntfy.sh, donde está cómo instalar la app y suscribirse.")
        open_docs.clicked.connect(self._open_ntfy_docs)
        actions.addWidget(open_docs)
        actions.addStretch(1)
        self._ntfy_test = self._test_button("Probar móvil", self._test_ntfy)
        actions.addWidget(self._ntfy_test)
        layout.addLayout(actions)

        self._ntfy_status = QLabel("")
        self._ntfy_status.setObjectName("muted")
        self._ntfy_status.setFont(theme.font(8))
        self._ntfy_status.setWordWrap(True)
        layout.addWidget(self._ntfy_status)

        guide = QLabel(
            "1. Instala la app ntfy en el móvil (Google Play, F-Droid o App Store).\n"
            "2. Pulsa «Generar tema seguro» y suscríbete en la app a ese mismo tema.\n"
            "3. Pulsa «Probar móvil»: la notificación debe llegar en segundos.\n"
            "En ntfy.sh el tema no es privado: quien lo conozca puede suscribirse y "
            "publicar. Para control de acceso real, usa tu propio servidor ntfy con un "
            "token, o en ntfy.sh crea un usuario y ponlo en el tema (configuración "
            "avanzada del tema en la app)."
        )
        guide.setObjectName("muted")
        guide.setWordWrap(True)
        guide.setFont(theme.font(8))
        layout.addWidget(guide)
        layout.addStretch(1)

        for widget in (self._ntfy_server, self._ntfy_topic):
            widget.textChanged.connect(self._refresh_ntfy_status)
        self._ntfy_enabled.toggled.connect(self._refresh_ntfy_status)
        return page

    def _priorities_row(self, values: dict[str, str], form: QFormLayout) -> dict[str, QComboBox]:
        """Las cuatro prioridades en una fila: se comparan de un vistazo.

        Cuatro filas de formulario para cuatro desplegables de cinco opciones
        dejaban el diálogo más alto que muchas pantallas. En una sola fila con
        el evento encima, la tabla se lee igual y cabe.
        """
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(2)
        columns = (
            ("triggered", "Disparo"),
            ("device_lost", "Sin sensor"),
            ("armed", "Armada"),
            ("disarmed", "Desarmada"),
        )
        combos: dict[str, QComboBox] = {}
        for column, (cause, caption) in enumerate(columns):
            label = QLabel(caption)
            label.setObjectName("muted")
            label.setFont(theme.font(8))
            grid.addWidget(label, 0, column)
            combo = QComboBox()
            for key in NTFY_PRIORITIES:
                combo.addItem(PRIORITY_LABELS.get(key, key), key)
            index = combo.findData(values[cause])
            if index >= 0:
                combo.setCurrentIndex(index)
            combo.setToolTip(PRIORITY_URGENT_HINT)
            grid.addWidget(combo, 1, column)
            combos[cause] = combo
        form.addRow("Prioridad", holder)
        return combos

    def _generate_topic(self) -> None:
        """Tema largo y aleatorio: el secreto del nombre es la única defensa en ntfy.sh."""
        self._ntfy_topic.setText(f"alarma-{token_urlsafe(10).lower()}")
        self._ntfy_topic.setFocus()
        self._refresh_ntfy_status()

    def _open_ntfy_docs(self) -> None:
        QDesktopServices.openUrl(QUrl(NTFY_DOCS))

    def _refresh_ntfy_status(self) -> None:
        """Dice qué falta y avisa si el tema queda expuesto en el servidor público."""
        config = self.build_config()
        ntfy = config.ntfy
        missing = ntfy.missing_fields
        if missing:
            self._ntfy_status.setText(f"Falta: {_join_fields(missing)}.")
            self._ntfy_status.setStyleSheet(f"color: {theme.WARN};")
            return
        warnings: list[str] = []
        if ntfy.uses_public_server:
            if len(ntfy.topic) < 12:
                warnings.append(
                    "en ntfy.sh un tema corto se puede adivinar: usa «Generar tema seguro»"
                )
            if not ntfy.token:
                warnings.append("el tema es público para quien conozca su nombre")
        if ntfy.base_url.startswith("http://"):
            warnings.append("la conexión no va cifrada (http)")
        if warnings:
            self._ntfy_status.setText("Aviso: " + " · ".join(warnings) + ".")
            self._ntfy_status.setStyleSheet(f"color: {theme.WARN};")
            return
        self._ntfy_status.setText("Listo para probar: el servidor y el tema están completos.")
        self._ntfy_status.setStyleSheet(f"color: {theme.OK};")

    def _test_ntfy(self) -> None:
        missing = self.build_config().ntfy.missing_fields
        if missing:
            self._tabs.setCurrentIndex(TAB_NTFY)
            QMessageBox.warning(
                self,
                "Faltan datos de ntfy",
                f"Para enviar un aviso de prueba falta: {_join_fields(missing)}.",
            )
            self._refresh_ntfy_status()
            return
        if self._on_test_ntfy is not None:
            self._on_test_ntfy(self.build_config())

    # ------------------------------------------------------------------
    # Utilidades de construccion
    # ------------------------------------------------------------------

    def _fit_initial_size(self) -> None:
        """Abre con sitio para la pestaña más alta que quepa en la pantalla.

        El ancho es fijo: las etiquetas con salto de línea piden un ancho enorme
        como «tamaño preferido» y el diálogo saldría más ancho que la ventana.
        """
        height = self.sizeHint().height()
        screen = QApplication.primaryScreen()
        if screen is not None:
            height = min(height, screen.availableGeometry().height() - 90)
        self.resize(900, max(self.minimumHeight(), height))

    def _events_row(
        self, armed: bool, disarmed: bool, triggered: bool, device_lost: bool = False
    ) -> tuple[QCheckBox, QCheckBox, QCheckBox, QCheckBox]:
        boxes = []
        for text, value, hint in (
            ("Armada", armed, "cuando la alarma pase a «armada»"),
            ("Desarmada", disarmed, "cuando la alarma pase a «desarmada»"),
            ("Disparada", triggered, "cuando el sensor detecte movimiento"),
            (
                "Sin sensor",
                device_lost,
                "cuando se pierda el enlace con la placa: la alarma deja de estar disponible",
            ),
        ):
            box = QCheckBox(text)
            box.setChecked(value)
            box.setToolTip(f"Generar un aviso {hint}.")
            boxes.append(box)
        return boxes[0], boxes[1], boxes[2], boxes[3]

    @staticmethod
    def _wrap_row(*widgets: QWidget) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        for widget in widgets:
            row.addWidget(widget)
        row.addStretch(1)
        return holder

    @staticmethod
    def _scrollable(page: QWidget) -> QScrollArea:
        """Pestaña con desplazamiento: el formulario puede ser más alto que la pantalla."""
        area = _ScrolledPage()
        area.setWidget(page)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        return area

    @staticmethod
    def _test_button(text: str, slot: Callable[[], None]) -> QWidget:
        button = QPushButton(text)
        button.setIcon(icons.icon("play", theme.ACCENT, 18))
        button.setToolTip("Envía un aviso de prueba con esta configuración, sin guardar.")
        button.clicked.connect(slot)
        return button

    def _refresh_provider(self) -> None:
        gateway = self._sms_provider.currentData() == "email_gateway"
        self._sms_from.setEnabled(not gateway)
        self._sms_gateway.setEnabled(gateway)
        self._sms_sid.setEnabled(not gateway)
        self._sms_token.setEnabled(not gateway)
        self._refresh_sms_status()

    def _refresh_counter(self) -> None:
        sample = self._sms_template.text().replace("{evento}", "disparada")
        sample = sample.replace("{hora}", "12:34")
        text = clip(sample)
        full = len(" ".join(sample.split()))
        self._sms_counter.setText(
            f"{len(text)} de {SMS_MAX_CHARS} caracteres"
            + (" · se recortará" if full > SMS_MAX_CHARS else "")
        )

    def _open_twilio(self) -> None:
        QDesktopServices.openUrl(QUrl(TWILIO_CONSOLE))

    def _refresh_sms_status(self) -> None:
        """Dice qué falta antes de probar, en vez de fallar con un texto vago."""
        config = self.build_config()
        missing = config.sms.missing_fields
        # Un numero tecleado y aun sin «Anadir» ya cuenta: el usuario lo ve.
        pending = normalize_number(self._sms_to.pending())
        if missing == ("destinatarios",) and pending:
            missing = ()
        if not missing:
            self._sms_status.setText("Listo para probar: los datos del proveedor están completos.")
            self._sms_status.setStyleSheet(f"color: {theme.OK};")
            return
        self._sms_status.setText(f"Falta: {_join_fields(missing)}.")
        self._sms_status.setStyleSheet(f"color: {theme.WARN};")

    # ------------------------------------------------------------------
    # Coherencia entre puerto y cifrado
    # ------------------------------------------------------------------

    def _sync_email_transport_initial(self, port: int, security: str) -> None:
        """Corrige al abrir una pareja imposible (465+STARTTLS, 587+SSL)."""
        fixed = effective_security(port, security)
        if fixed == security or fixed == "none":
            return
        self._transport_sync = True
        try:
            self._email_port.setValue(SSL_PORT if fixed == "ssl" else STARTTLS_PORT)
            self._select_email_security(fixed)
        finally:
            self._transport_sync = False

    def _select_email_security(self, value: str) -> None:
        index = self._email_security.findData(value)
        if index >= 0:
            self._email_security.setCurrentIndex(index)

    def _on_email_port_changed(self) -> None:
        """465 encaja con SSL y 587 con STARTTLS; el resto se deja como está."""
        if self._transport_sync:
            return
        port = self._email_port.value()
        if port == SSL_PORT:
            self._select_email_security("ssl")
        elif port == STARTTLS_PORT:
            self._select_email_security("starttls")

    def _on_email_security_changed(self) -> None:
        """Cambiar de modo mueve el puerto solo si era el del otro modo."""
        if self._transport_sync:
            return
        security = str(self._email_security.currentData())
        port = self._email_port.value()
        target: int | None = None
        if security == "ssl" and port == STARTTLS_PORT:
            target = SSL_PORT
        elif security == "starttls" and port == SSL_PORT:
            target = STARTTLS_PORT
        if target is None:
            return
        self._transport_sync = True
        try:
            self._email_port.setValue(target)
        finally:
            self._transport_sync = False

    # ------------------------------------------------------------------
    # Datos
    # ------------------------------------------------------------------

    def build_config(self) -> AppConfig:
        def events(boxes: tuple[QCheckBox, ...]) -> tuple[bool, bool, bool, bool]:
            return (
                boxes[0].isChecked(),
                boxes[1].isChecked(),
                boxes[2].isChecked(),
                boxes[3].isChecked(),
            )

        email_armed, email_disarmed, email_triggered, email_lost = events(
            (self._email_armed, self._email_disarmed, self._email_triggered, self._email_lost)
        )
        email = replace(
            self._config.email,
            enabled=self._email_enabled.isChecked(),
            on_armed=email_armed,
            on_disarmed=email_disarmed,
            on_triggered=email_triggered,
            on_device_lost=email_lost,
            host=self._email_host.text().strip(),
            port=self._email_port.value(),
            security=effective_security(
                self._email_port.value(), str(self._email_security.currentData())
            ),
            sender=self._email_from.text().strip(),
            to=tuple(self._email_to.values()),
            subject=self._email_subject.text().strip() or "[Alarma] {evento} a las {hora}",
            timeout_s=self._email_timeout.value(),
            retries=self._email_retries.value(),
            user=self._email_user.text().strip(),
            password=self._email_password.text() or self._config.email.password,
        )

        sms_armed, sms_disarmed, sms_triggered, sms_lost = events(
            (self._sms_armed, self._sms_disarmed, self._sms_triggered, self._sms_lost)
        )
        numbers = [normalize_number(n) for n in self._sms_to.values()]
        sms = replace(
            self._config.sms,
            enabled=self._sms_enabled.isChecked(),
            provider=str(self._sms_provider.currentData()),
            on_armed=sms_armed,
            on_disarmed=sms_disarmed,
            on_triggered=sms_triggered,
            on_device_lost=sms_lost,
            to=tuple(n for n in numbers if n),
            from_number=normalize_number(self._sms_from.text().strip()),
            gateway_domain=self._sms_gateway.text().strip(),
            template=self._sms_template.text().strip() or "[Alarma] {evento} a las {hora}",
            timeout_s=self._sms_timeout.value(),
            retries=self._sms_retries.value(),
            sid=self._sms_sid.text().strip() or self._config.sms.sid,
            token=self._sms_token.text() or self._config.sms.token,
        )

        ntfy_armed, ntfy_disarmed, ntfy_triggered, ntfy_lost = events(
            (self._ntfy_armed, self._ntfy_disarmed, self._ntfy_triggered, self._ntfy_lost)
        )
        ntfy = replace(
            self._config.ntfy,
            enabled=self._ntfy_enabled.isChecked(),
            server=self._ntfy_server.text().strip() or "https://ntfy.sh",
            topic=self._ntfy_topic.text().strip(),
            on_armed=ntfy_armed,
            on_disarmed=ntfy_disarmed,
            on_triggered=ntfy_triggered,
            on_device_lost=ntfy_lost,
            title=self._ntfy_title.text().strip() or "[Alarma] {evento}",
            priority_triggered=str(self._ntfy_priority_triggered.currentData()),
            priority_device_lost=str(self._ntfy_priority_lost.currentData()),
            priority_armed=str(self._ntfy_priority_armed.currentData()),
            priority_disarmed=str(self._ntfy_priority_disarmed.currentData()),
            timeout_s=self._ntfy_timeout.value(),
            retries=self._ntfy_retries.value(),
            token=self._ntfy_token.text() or self._config.ntfy.token,
        )
        return replace(self._config, email=email, sms=sms, ntfy=ntfy)

    def secrets(self) -> dict[str, str]:
        """Credenciales que hay que escribir en `.env`. Vacias = sin cambios."""
        values: dict[str, str] = {}
        user = self._email_user.text().strip()
        password = self._email_password.text()
        sid = self._sms_sid.text().strip()
        token = self._sms_token.text()
        ntfy_token = self._ntfy_token.text()
        if user:
            values["ALARM_SMTP_USER"] = user
        if password:
            values["ALARM_SMTP_PASSWORD"] = password
        if sid:
            values["ALARM_TWILIO_SID"] = sid
        if token:
            values["ALARM_TWILIO_TOKEN"] = token
        if ntfy_token:
            values["ALARM_NTFY_TOKEN"] = ntfy_token
        return values

    def _validate(self) -> str:
        config = self.build_config()
        if config.email.enabled:
            if not config.email.host:
                return "Falta el servidor SMTP."
            if not config.email.sender:
                return "Falta el remitente del correo."
            if not config.email.to:
                return "Añade al menos un destinatario de correo."
            if not config.email.user:
                return "Falta el usuario SMTP."
            if not config.email.password:
                return "Falta la contraseña SMTP (se guarda en .env)."
        if config.sms.enabled:
            missing = config.sms.missing_fields
            if missing:
                return f"Faltan datos del SMS: {_join_fields(missing)}."
            if config.sms.provider == "email_gateway" and (
                not config.email.user or not config.email.password
            ):
                return "La pasarela reutiliza el SMTP: faltan usuario y contraseña."
        if config.ntfy.enabled:
            missing = config.ntfy.missing_fields
            if missing:
                return f"Faltan datos del aviso al móvil: {_join_fields(missing)}."
            if not valid_ntfy_topic(config.ntfy.topic):
                return (
                    "El tema de ntfy solo admite letras, números, guion y guion bajo "
                    "(1 a 64 caracteres)."
                )
            if not normalize_ntfy_server(config.ntfy.server):
                return "El servidor de ntfy debe ser una dirección http(s)."
        return ""

    # ------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------

    def _test_email(self) -> None:
        if not self._email_to.commit_pending():
            QMessageBox.warning(self, "Destinatario no válido", self._email_to.error_text())
            return
        if self._on_test_email is not None:
            self._on_test_email(self.build_config())

    def _test_sms(self) -> None:
        if not self._sms_to.commit_pending():
            QMessageBox.warning(self, "Número no válido", self._sms_to.error_text())
            return
        missing = self.build_config().sms.missing_fields
        if missing:
            self._tabs.setCurrentIndex(TAB_SMS)
            QMessageBox.warning(
                self,
                "Faltan datos del SMS",
                f"Para enviar un SMS de prueba falta: {_join_fields(missing)}.",
            )
            self._refresh_sms_status()
            return
        if self._on_test_sms is not None:
            self._on_test_sms(self.build_config())

    def accept(self) -> None:
        self._email_to.commit_pending()
        self._sms_to.commit_pending()
        problem = self._validate()
        if problem:
            self._tabs.setCurrentIndex(_tab_for(problem))
            QMessageBox.warning(self, "Faltan datos", problem)
            return
        try:
            self._on_save(self.build_config(), self.secrets())
        except Exception as exc:  # noqa: BLE001 - se muestra al usuario
            QMessageBox.critical(
                self, "No se pudo guardar", f"La configuración no se ha guardado:\n{exc}"
            )
            return
        super().accept()


def _looks_like_email(value: str) -> bool:
    return valid_email(value)


def _tab_for(problem: str) -> int:
    """Pestana donde esta el dato que falta, deducida del texto del aviso."""
    text = problem.lower()
    if "móvil" in text or "ntfy" in text or "tema" in text:
        return TAB_NTFY
    if "correo" in text or "smtp" in text:
        return TAB_EMAIL
    return TAB_SMS


def _join_fields(fields: tuple[str, ...]) -> str:
    """Enumera los campos pendientes como se leen, no como una lista de codigo."""
    if len(fields) <= 1:
        return "".join(fields)
    return ", ".join(fields[:-1]) + " y " + fields[-1]


__all__ = ["NotificationsDialog", "PROVIDER_LABELS"]
