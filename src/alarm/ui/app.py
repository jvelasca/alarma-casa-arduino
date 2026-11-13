"""Ventana principal de la alarma, en PyQt6.

La ventana **no** toca el puerto serie. El `DeviceManager` lee en su propio hilo
y reemite los mensajes por `QtBridge`; Qt los entrega en el hilo de la interfaz.

La pantalla se ordena por frecuencia de uso: primero el estado y el boton grande
de armar/desarmar, despues el dispositivo y al final el registro. Todo lo que se
toca una vez al mes (textos de voz, credenciales, ajuste del sensor) vive en
dialogos y menus, no en la pantalla principal.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QAction, QCloseEvent, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..backup import (
    EXTENSION,
    Backup,
    BackupError,
    build_backup,
    collect_secrets,
    read_backup,
    write_backup,
)
from ..backup import (
    restore as backup_restore,
)
from ..config import (
    CONFIG_FILE,
    ENV_FILE,
    LOCAL_FILE,
    STORAGE_ROOT,
    AppConfig,
    ConfigError,
    default_config,
    load_config,
    load_env,
)
from ..config_export import export_config_json
from ..core.state import (
    ACTION_ARMED,
    ACTION_ARMING,
    ACTION_DISARMED,
    ACTION_PENDING,
    ACTION_REARM_EXHAUSTED,
    ACTION_REARMED,
    ACTION_REARMING,
    ACTION_SILENCED,
    ACTION_TRIGGERED,
    AlarmController,
    AlarmState,
    Transition,
)
from ..device.manager import DeviceManager
from ..paths import fallback_used
from ..protocol.messages import (
    DeviceError,
    Health,
    Heartbeat,
    Hello,
    Message,
    MotionEvent,
    MotionKind,
    Ready,
)
from ..secrets import update_env
from ..services.base import Channel, DeliveryResult
from ..services.email_sender import EmailChannel
from ..services.notifier import Notifier
from ..services.ntfy_commands import (
    CommandListener,
    RemoteCommand,
    RemoteState,
    StatusInfo,
    format_status,
)
from ..services.ntfy_sender import NtfyChannel
from ..services.sms_sender import build_sms_channel
from ..services.storage import EventStore
from ..services.tts import SpeechService
from ..settings_store import save_local
from ..transport.base import Link
from ..transport.serial_link import SerialLink, describe_ports, list_candidates
from ..transport.simulated_link import SimulatedLink
from . import icons, theme
from .audio import beep_async, set_enabled
from .bridge import QtBridge
from .calibration import CalibrationDialog
from .dialogs.about import AboutDialog
from .dialogs.buzzer import BuzzerDialog
from .dialogs.delays import DelaysDialog
from .dialogs.notifications import NotificationsDialog
from .dialogs.remote import RemoteDialog
from .dialogs.result import ResultDialog
from .dialogs.voice_config import VoiceConfigDialog
from .sensor_panel import SensorPanel
from .widgets import Card, EventLog, IconButton, StateHero, tip

log = logging.getLogger(__name__)

APP_TITLE = "Alarma de presencia — Arduino Mega 2560"

_HEALTH_STYLE: dict[Health, tuple[str, str]] = {
    Health.ONLINE: ("Conectado", theme.OK),
    Health.DEGRADED: ("Enlace lento", theme.WARN),
    Health.OFFLINE: ("Sin respuesta", theme.FAIL),
    Health.UNKNOWN: ("Iniciando…", theme.MUTED),
}

_STATE_KEYS: dict[AlarmState, str] = {
    AlarmState.DISARMED: "disarmed",
    AlarmState.ARMING: "arming",
    AlarmState.ARMED: "armed",
    AlarmState.PENDING: "pending",
    AlarmState.TRIGGERED: "triggered",
    AlarmState.REARMING: "rearming",
}

_STATE_HINTS: dict[str, str] = {
    "disarmed": "La alarma está apagada y no avisará de movimiento.",
    "arming": "Retardo de salida: sal de casa. La alarma se armará al terminar.",
    "armed": "Vigilando. Si el sensor detecta movimiento, se disparará.",
    "pending": "Presencia detectada. Desarma antes de que se dispare la alarma.",
    "triggered": "Presencia detectada. Silencia o desarma para detener los avisos.",
    "rearming": "Tras un disparo: la alarma volverá a vigilar sola al terminar la cuenta.",
    "silenced": "Alarma silenciada: sigue armada y vigilando.",
}

# Se anade a la pista de estado cuando el enlace esta caido: la alarma no
# puede cumplir lo que promete el estado, y callarlo seria peor que avisar.
_LINK_DOWN_HINT = "Sin sensor: la alarma no está disponible."

#: Indicadores de la barra de estado: icono, nombre legible y donde se cambia.
#: Se pintan siempre los seis, encendidos o apagados: saber que un aviso esta
#: apagado importa tanto como saber que esta encendido.
_CHANNELS: dict[str, tuple[str, str, str]] = {
    "voz": ("mic", "Voz por el altavoz", "Avisos → Voz por el altavoz"),
    "correo": ("mail", "Aviso por correo", "Avisos → Aviso por correo"),
    "sms": ("sms", "Aviso por SMS", "Avisos → Aviso por SMS"),
    "movil": ("phone", "Aviso al móvil (ntfy)", "Avisos → Aviso al móvil (ntfy)"),
    "mando": ("key", "Mando remoto (ntfy)", "Avisos → Mando remoto (ntfy)…"),
    "zumbador": ("bell", "Zumbador de la placa", "Alarma → Zumbador…"),
}

#: Nombre de cada canal en el registro de resultados del envio.
_CHANNEL_RESULT_LABELS: dict[str, str] = {
    "email": "correo",
    "sms": "SMS",
    "ntfy": "móvil",
}


def _menu(parent: QMenuBar, title: str) -> QMenu:
    """Crea un menu garantizando que no es nulo (los stubs lo declaran opcional)."""
    menu = parent.addMenu(title)
    assert menu is not None  # noqa: S101 - garantia del propio Qt
    return menu


def _thousands(value: int) -> str:
    """Separador de miles espanol: 100.000, no 100,000."""
    return f"{value:,}".replace(",", ".")


def _span(seconds: float) -> str:
    """Duracion legible: 90 s se dice «1 min 30 s», no «90 s».

    Las esperas de rearme se configuran en minutos, pero el valor viaja en
    segundos; leerlos siempre en segundos obligaria a dividir de cabeza.
    """
    total = int(round(seconds))
    if total <= 0:
        return "0 s"
    if total < 60:
        return f"{total} s"
    minutes, rest = divmod(total, 60)
    if not rest:
        return f"{minutes} min"
    return f"{minutes} min {rest} s"


def _delay_text(seconds: float) -> str:
    """Retardo en segundos para la respuesta al movil; 0 se dice con palabras."""
    return f"{seconds:g} s" if seconds > 0 else "sin retardo"


def _as_int(value: object) -> int | None:
    """Convierte un campo del `hello` a entero, o None si no es un numero."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _retention_text(days: int) -> str:
    if days % 365 == 0 and days >= 365:
        years = days // 365
        return "1 año" if years == 1 else f"{years} años"
    return f"{days} días"


class MainWindow(QMainWindow):
    """Pantalla unica: estado grande, dispositivo y registro."""

    REFRESH_MS = 500
    DELAY_TICK_MS = 250
    PORT_REFRESH_MS = 3_000
    PURGE_MS = 24 * 60 * 60 * 1_000

    def __init__(
        self,
        *,
        simulated: bool = False,
        port: str | None = None,
        baud: int = 115200,
        sim_speed: float = 1.0,
        autoconnect: bool = True,
        config: AppConfig | None = None,
        config_path: Path | None = None,
        local_path: Path | None = None,
        env_path: Path | None = None,
        history_path: Path | str | None = None,
    ) -> None:
        super().__init__()
        self._baud = baud
        self._sim_speed = sim_speed
        self._preferred_port = port
        self._config_path = CONFIG_FILE if config_path is None else Path(config_path)
        self._local_path = LOCAL_FILE if local_path is None else Path(local_path)
        self._env_path = ENV_FILE if env_path is None else Path(env_path)

        self._manager: DeviceManager | None = None
        self._calib_dialog: CalibrationDialog | None = None
        self._port_map: dict[str, str] = {}
        self._pending_test: dict[str, AppConfig] = {}
        self._events = 0
        self._pulses = 0
        self._level = False

        # Estado del enlace, para no repetir avisos en cada refresco.
        self._link_down = False
        self._link_lost = False
        self._expect_disconnect = False
        self._last_health: Health | None = None
        self._link_name = ""
        self._connection_state = ""
        self._store_error = ""

        # Calentamiento del PIR: total, ultima marca de `up_ms` y cuando llego.
        self._warmup_total_ms = 0
        self._warmup_base_ms = 0
        self._warmup_synced_at = time.monotonic()

        # Pin real del zumbador segun la placa; el del fichero es solo informativo.
        self._device_buzzer_pin: int | None = None

        # Mando remoto: el listener se crea y se destruye con la configuracion.
        # Se declara antes de la barra de estado, que ya pinta su indicador.
        self._remote_listener: CommandListener | None = None

        self._config = config if config is not None else default_config()
        self._controller = AlarmController(
            armed_on_start=self._config.alarm.armed_on_start,
            cooldown_s=self._config.alarm.cooldown_s,
            max_per_hour=self._config.alarm.max_per_hour,
            exit_delay_s=self._config.alarm.exit_delay_s,
            entry_delay_s=self._config.alarm.entry_delay_s,
            auto_rearm=self._config.alarm.auto_rearm,
            rearm_delay_s=self._config.alarm.rearm_delay_s,
            max_rearms=self._config.alarm.max_rearms,
            siren_s=self._config.buzzer.siren_ms / 1000,
        )
        self._speech = SpeechService(self._config.tts)
        self._email = EmailChannel(self._config.email)
        self._sms = build_sms_channel(self._config.sms, self._email)
        self._ntfy = NtfyChannel(self._config.ntfy)
        self._notifier = Notifier(
            [self._email, self._sms, self._ntfy],
            gate=self._controller.allow,
            on_result=self._on_notify_result,
        )

        self._bridge = QtBridge()
        self._bridge.message.connect(self._on_message)
        self._bridge.health.connect(self._on_health)
        self._bridge.notify_result.connect(self._on_notify_result_main)
        self._bridge.test_finished.connect(self._on_test_finished)
        self._bridge.remote_command.connect(self._on_remote_command)
        self._bridge.remote_state.connect(self._on_remote_state)
        self._bridge.remote_log.connect(self._on_remote_log)

        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(icons.app_icon())
        self.resize(1020, 760)
        # El mínimo tiene que ser el que de verdad necesita el contenido: por
        # debajo, Qt reparte menos altura de la que piden las filas y la
        # etiqueta del calentamiento se dibuja encima de la barra y de los
        # contadores. Se mide con la hoja de estilos aplicada (fuente incluida).
        self.setMinimumSize(880, 750)

        self._store = self._open_store(history_path)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_central(simulated)
        self._build_statusbar()

        self._populate_ports()
        self._paint_alarm()
        self._apply_health(Health.UNKNOWN)
        set_enabled(self._config.ui.beeps)
        self._log.set_limit(self._config.log.visible_rows)
        self._load_history()
        if self._store_error:
            self._log_event(
                "error", "Historial", f"no se pudo abrir la base de datos: {self._store_error}"
            )
            self._status("Historial en memoria: no se pudo abrir la base de datos.")
        elif fallback_used():
            # Un .exe dentro de Archivos de programa no puede escribir a su lado:
            # los ajustes acaban en el perfil del usuario y hay que decirlo, o el
            # usuario buscara `config/` junto al ejecutable y no lo encontrara.
            self._log_event(
                "warning",
                "Datos",
                f"la carpeta del ejecutable no admite escritura; configuración e historial "
                f"en {STORAGE_ROOT}",
            )

        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        # Los retardos son estado de la alarma, no del enlace: su reloj corre
        # aunque la placa este desconectada o simulada. Cuatro veces por segundo
        # basta para que el contador no parpadee y el vencimiento sea puntual.
        self._delay_timer = QTimer(self)
        self._delay_timer.setInterval(self.DELAY_TICK_MS)
        self._delay_timer.timeout.connect(self._tick_delays)
        self._delay_timer.start()

        # Enchufar la placa despues de arrancar debe funcionar sin reiniciar:
        # solo se enumeran puertos cuando aun no hay enlace, para no pelearse
        # con la seleccion del usuario.
        self._port_timer = QTimer(self)
        self._port_timer.setInterval(self.PORT_REFRESH_MS)
        self._port_timer.timeout.connect(self._refresh_ports_if_idle)
        self._port_timer.start()

        # La retencion tambien se aplica con la aplicacion abierta muchos dias.
        self._purge_timer = QTimer(self)
        self._purge_timer.setInterval(self.PURGE_MS)
        self._purge_timer.timeout.connect(self._purge_history)
        self._purge_timer.start()

        # El mando remoto se enciende al final: necesita la barra de estado ya
        # construida para pintar su indicador y el puente ya conectado.
        self._sync_remote_listener()

        if autoconnect and self._config.serial.auto_connect:
            QTimer.singleShot(150, self.connect)

    # ------------------------------------------------------------------
    # Acciones, menus y barra
    # ------------------------------------------------------------------

    def _action(
        self,
        text: str,
        slot: Callable[[], object],
        *,
        icon_name: str = "",
        shortcut: str = "",
        tooltip: str = "",
        color: str = theme.TEXT,
    ) -> QAction:
        action = QAction(text, self)
        if icon_name:
            action.setIcon(icons.icon(icon_name, color, 22))
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        action.setToolTip(tooltip or text)
        action.setStatusTip(tooltip or text)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _build_actions(self) -> None:
        self.act_arm = self._action(
            "Armar",
            self.arm,
            icon_name="lock",
            shortcut="Ctrl+Shift+A",
            tooltip="Activa la vigilancia. Equivale al botón grande de la pantalla.",
            color=theme.OK,
        )
        self.act_disarm = self._action(
            "Desarmar",
            self.disarm,
            icon_name="unlock",
            shortcut="Ctrl+D",
            tooltip="Apaga la vigilancia y anula los avisos pendientes.",
        )
        self.act_silence = self._action(
            "Silenciar",
            self.silence,
            icon_name="bell_off",
            shortcut="Ctrl+Shift+M",
            tooltip="Calla la sirena sin desarmar: la alarma sigue vigilando.",
            color=theme.WARN,
        )
        self.act_connect = self._action(
            "Conectar",
            self.connect,
            icon_name="usb",
            shortcut="Ctrl+R",
            tooltip="Abre el puerto serie o reconecta con el dispositivo simulado.",
        )
        self.act_disconnect = self._action(
            "Desconectar",
            self.disconnect,
            icon_name="power",
            tooltip="Cierra el enlace con la placa.",
        )
        self.act_calibration = self._action(
            "Ajuste del sensor…",
            self.open_calibration,
            icon_name="activity",
            shortcut="Ctrl+A",
            tooltip="Mide la respuesta del PIR y dice qué potenciómetro girar.",
        )
        self.act_voice_try = self._action(
            "Probar voz",
            self.test_voice,
            icon_name="mic",
            tooltip="Pronuncia un mensaje de ejemplo con la voz configurada.",
        )
        self.act_voice_config = self._action(
            "Configuración de voces…",
            self.open_voice_config,
            icon_name="mic",
            shortcut="Ctrl+Alt+V",
            tooltip="Texto, repeticiones, retardos y registro de cada aviso hablado.",
        )
        self.act_notifications = self._action(
            "Correo, SMS y móvil…",
            self.open_notifications,
            icon_name="mail",
            tooltip="Destinatarios, eventos y credenciales de los avisos remotos.",
        )
        self.act_reload = self._action(
            "Recargar configuración",
            self.reload_config,
            icon_name="refresh",
            tooltip="Relee config/alarm.toml y config/alarm.local.toml sin reiniciar.",
        )
        self.act_clear = self._action(
            "Limpiar registro…",
            self._confirm_clear_log,
            icon_name="trash",
            shortcut="Ctrl+L",
            tooltip="Vacía el historial y pone los contadores a cero. Pide confirmación.",
        )
        self.act_export = self._action(
            "Exportar registro…",
            self.export_log,
            icon_name="download",
            shortcut="Ctrl+E",
            tooltip="Guarda todo el historial en un CSV que Excel abre con acentos.",
        )
        self.act_export_config = self._action(
            "Guardar configuración en JSON…",
            self.export_config,
            icon_name="save",
            shortcut="Ctrl+Shift+C",
            tooltip="Vuelca toda la configuración a un JSON sin credenciales, para consultarla "
            "o adjuntarla a un informe.",
        )
        self.act_backup = self._action(
            "Guardar copia completa (con credenciales)…",
            self.export_backup,
            icon_name="lock",
            shortcut="Ctrl+Shift+B",
            tooltip="Un solo fichero con toda la configuración, el puerto serie y las "
            "contraseñas de correo, SMS y ntfy, para montar la alarma en otro PC. "
            "Contiene secretos en claro: guárdalo como guardarías una contraseña.",
        )
        self.act_restore = self._action(
            "Restaurar copia completa…",
            self.restore_backup,
            icon_name="unlock",
            shortcut="Ctrl+Shift+R",
            tooltip="Aplica en este PC la configuración y las credenciales de una copia "
            "hecha con «Guardar copia completa».",
        )
        self.act_buzzer = self._action(
            "Probar zumbador",
            self.test_buzzer,
            icon_name="bell",
            tooltip="Envía a la placa un patrón corto para comprobar el zumbador.",
        )
        self.act_buzzer_config = self._action(
            "Zumbador…",
            self.open_buzzer_config,
            icon_name="bell",
            shortcut="Ctrl+Alt+B",
            tooltip="Tono continuo para comprobar el cableado, cada patrón de la placa y la "
            "duración de la sirena.",
        )
        self.act_delays = self._action(
            "Retardos y rearme…",
            self.open_delays_config,
            icon_name="clock",
            shortcut="Ctrl+Alt+R",
            tooltip="Cuánto tarda en vigilar al armar, cuánto espera antes de dispararse y "
            "cuántas veces vuelve a armarse sola tras un disparo.",
        )
        self.act_skip_warmup = self._action(
            "Omitir calentamiento del PIR",
            self.skip_warmup,
            icon_name="play",
            tooltip="Da el sensor por listo. Útil si la placa lleva un minuto encendida.",
        )
        self.act_about = self._action(
            "Acerca de…",
            self.open_about,
            icon_name="info",
            shortcut="F1",
            tooltip="Versión, hardware y correo de contacto.",
        )
        self.act_contact = self._action(
            "Contacto…",
            self.open_about,
            icon_name="mail",
            tooltip="Correo del autor con enlace y botón de copiar.",
        )
        self.act_quit = self._action(
            "Salir",
            self.close,
            shortcut="Alt+F4",
            tooltip="Cierra la aplicación de forma ordenada.",
        )

        self.act_show_heartbeats = QAction("Mostrar latidos", self)
        self.act_show_heartbeats.setCheckable(True)
        self.act_show_heartbeats.setChecked(self._config.ui.show_heartbeats)
        self.act_show_heartbeats.setToolTip(
            "Añade al registro una línea por cada latido recibido de la placa."
        )
        self.act_show_heartbeats.toggled.connect(self._on_toggle_heartbeats)

        self.act_beeps = QAction("Pitidos al ajustar", self)
        self.act_beeps.setCheckable(True)
        self.act_beeps.setChecked(self._config.ui.beeps)
        self.act_beeps.setToolTip("Avisos sonoros que guían las fases del ajuste del sensor.")
        self.act_beeps.toggled.connect(self._on_toggle_beeps)

        self.act_tts = QAction("Voz por el altavoz", self)
        self.act_tts.setCheckable(True)
        self.act_tts.setChecked(self._config.tts.enabled)
        self.act_tts.setToolTip("Activa o desactiva todos los mensajes hablados.")
        self.act_tts.toggled.connect(self._on_toggle_tts)

        self.act_email = QAction("Aviso por correo", self)
        self.act_email.setCheckable(True)
        self.act_email.setChecked(self._config.email.enabled)
        self.act_email.setToolTip("Activa o desactiva el envío de correo.")
        self.act_email.toggled.connect(self._on_toggle_email)

        self.act_sms = QAction("Aviso por SMS", self)
        self.act_sms.setCheckable(True)
        self.act_sms.setChecked(self._config.sms.enabled)
        self.act_sms.setToolTip("Activa o desactiva el envío de SMS.")
        self.act_sms.toggled.connect(self._on_toggle_sms)

        self.act_ntfy = QAction("Aviso al móvil (ntfy)", self)
        self.act_ntfy.setCheckable(True)
        self.act_ntfy.setChecked(self._config.ntfy.enabled)
        self.act_ntfy.setToolTip("Notificaciones push gratuitas al móvil con la aplicación ntfy.")
        self.act_ntfy.toggled.connect(self._on_toggle_ntfy)

        self.act_remote = self._action(
            "Mando remoto (ntfy)…",
            self.open_remote,
            icon_name="key",
            tooltip="Órdenes desde el móvil: clave, TOTP, estado y últimas órdenes recibidas.",
        )

    def _build_menus(self) -> None:
        bar = QMenuBar(self)
        self.setMenuBar(bar)

        archivo = _menu(bar, "&Archivo")
        archivo.addAction(self.act_connect)
        archivo.addAction(self.act_disconnect)
        archivo.addSeparator()
        archivo.addAction(self.act_export)
        archivo.addAction(self.act_export_config)
        archivo.addSeparator()
        archivo.addAction(self.act_backup)
        archivo.addAction(self.act_restore)
        archivo.addSeparator()
        archivo.addAction(self.act_quit)

        alarma = _menu(bar, "&Alarma")
        alarma.addAction(self.act_arm)
        alarma.addAction(self.act_disarm)
        alarma.addAction(self.act_silence)
        alarma.addSeparator()
        alarma.addAction(self.act_buzzer)
        alarma.addAction(self.act_buzzer_config)
        alarma.addAction(self.act_delays)

        herramientas = _menu(bar, "&Herramientas")
        herramientas.addAction(self.act_calibration)
        herramientas.addAction(self.act_skip_warmup)
        herramientas.addSeparator()
        herramientas.addAction(self.act_voice_config)
        herramientas.addAction(self.act_buzzer_config)
        herramientas.addSeparator()
        herramientas.addAction(self.act_export)
        herramientas.addAction(self.act_export_config)
        herramientas.addAction(self.act_backup)
        herramientas.addAction(self.act_restore)
        herramientas.addAction(self.act_clear)

        avisos = _menu(bar, "A&visos")
        avisos.addAction(self.act_voice_config)
        avisos.addAction(self.act_notifications)
        avisos.addSeparator()
        avisos.addAction(self.act_tts)
        avisos.addAction(self.act_email)
        avisos.addAction(self.act_sms)
        avisos.addAction(self.act_ntfy)
        avisos.addAction(self.act_remote)
        avisos.addSeparator()
        avisos.addAction(self.act_voice_try)
        avisos.addAction(self.act_reload)

        ver = _menu(bar, "&Ver")
        ver.addAction(self.act_show_heartbeats)
        ver.addAction(self.act_beeps)

        ayuda = _menu(bar, "A&yuda")
        ayuda.addAction(self.act_about)
        ayuda.addAction(self.act_contact)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Principal", self)
        bar.setObjectName("mainToolbar")
        bar.setIconSize(QSize(22, 22))
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(bar)

        bar.addAction(self.act_arm)
        bar.addAction(self.act_disarm)
        bar.addAction(self.act_silence)
        bar.addSeparator()
        bar.addAction(self.act_calibration)
        bar.addAction(self.act_buzzer)
        bar.addSeparator()
        bar.addAction(self.act_voice_try)
        bar.addAction(self.act_voice_config)
        bar.addAction(self.act_notifications)
        bar.addSeparator()
        bar.addAction(self.act_connect)
        bar.addAction(self.act_reload)
        bar.addAction(self.act_export)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        about = IconButton(
            "info",
            "",
            "Acerca de la aplicación: versión, hardware y contacto.",
            color=theme.MUTED,
        )
        about.clicked.connect(self.open_about)
        bar.addWidget(about)
        self._toolbar = bar

    # ------------------------------------------------------------------
    # Cuerpo
    # ------------------------------------------------------------------

    def _build_central(self, simulated: bool) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(icons.app_icon().pixmap(34, 34))
        logo.setFixedSize(34, 34)
        header.addWidget(logo)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        name = QLabel("Alarma de presencia")
        name.setFont(theme.font(13, bold=True))
        titles.addWidget(name)
        subtitle = QLabel("Arduino Mega 2560 · sensor HC-SR501")
        subtitle.setObjectName("muted")
        subtitle.setFont(theme.font(8))
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch(1)

        self._sim_check = QCheckBox("Simulado")
        self._sim_check.setChecked(simulated)
        self._sim_check.setToolTip(
            "Genera un dispositivo virtual con movimiento periódico. No necesita hardware."
        )
        self._sim_check.toggled.connect(self._on_toggle_sim)
        header.addWidget(self._sim_check)

        outer.addLayout(header)

        top = QHBoxLayout()
        top.setSpacing(12)

        self._hero = StateHero()
        top.addWidget(self._hero, stretch=3)

        self._primary = QPushButton("Armar")
        self._primary.setObjectName("primary")
        self._primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self._primary.setMinimumHeight(58)
        self._primary.clicked.connect(self._primary_action)
        tip(
            self._primary,
            "Botón principal. Con la alarma apagada la arma; con la alarma puesta o "
            "disparada, la desarma.",
        )
        self._hero.add_button(self._primary)

        self._silence_button = QPushButton("Silenciar")
        self._silence_button.setIcon(icons.icon("bell_off", theme.WARN, 20))
        self._silence_button.setMinimumHeight(58)
        self._silence_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._silence_button.clicked.connect(self.silence)
        tip(self._silence_button, "Calla la sirena y la voz sin desarmar (Ctrl+Mayús+M).")
        self._hero.add_button(self._silence_button)

        self._siren_button = QPushButton("Probar sirena")
        self._siren_button.setIcon(icons.icon("bell", theme.MUTED, 20))
        self._siren_button.setMinimumHeight(58)
        self._siren_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._siren_button.clicked.connect(self.test_buzzer)
        tip(self._siren_button, "Envía a la placa el patrón de sirena configurado.")
        self._hero.add_button(self._siren_button)

        top.addWidget(self._build_device_card(), stretch=2)
        outer.addLayout(top, stretch=0)

        log_card = Card("Registro de eventos")
        log_actions = QWidget()
        log_row = QHBoxLayout(log_actions)
        log_row.setContentsMargins(0, 0, 0, 0)
        log_row.setSpacing(6)

        export = QPushButton("Exportar CSV")
        export.setIcon(icons.icon("download", theme.ACCENT, 18))
        export.setObjectName("link")
        export.clicked.connect(self.export_log)
        tip(
            export,
            "Guarda todo el historial (no solo lo que se ve) en un CSV con acentos "
            "legibles por Excel. También con Ctrl+E.",
        )
        log_row.addWidget(export)

        clear = QPushButton("Limpiar…")
        clear.setIcon(icons.icon("trash", theme.FAIL, 18))
        clear.setObjectName("link")
        clear.clicked.connect(self._confirm_clear_log)
        tip(
            clear,
            "Borra todo el historial, también el guardado en disco. Pide confirmación. "
            "También con Ctrl+L.",
        )
        log_row.addWidget(clear)

        log_card.add_header(log_actions)
        self._log = EventLog()
        # El registro es lo que cede altura cuando la ventana llega al mínimo;
        # la tarjeta del dispositivo y su calentamiento no se pueden comprimir.
        self._log.setMinimumHeight(40)
        log_card.add(self._log, stretch=1)
        self._log_footer = QLabel("")
        self._log_footer.setObjectName("muted")
        self._log_footer.setFont(theme.font(8))
        log_card.add(self._log_footer)
        outer.addWidget(log_card, stretch=1)

        self.setCentralWidget(central)
        self._central = central

    def _build_device_card(self) -> Card:
        card = Card("Dispositivo")

        # Estado del enlace en la propia tarjeta: la cabecera ya solo lleva la
        # casilla «Simulado», sin repetir el mismo «Conectado» dos veces.
        self._link_status = QLabel("Sin enlace")
        self._link_status.setObjectName("muted")
        self._link_status.setFont(theme.font(8))
        tip(
            self._link_status,
            "Salud del enlace con la placa y antigüedad del último latido.",
        )
        card.add_header(self._link_status)

        port_row = QHBoxLayout()
        port_row.setSpacing(6)
        self._port_box = QComboBox()
        self._port_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._port_box.setMinimumWidth(190)
        tip(
            self._port_box,
            "Puertos serie detectados. La placa aparece como «Arduino Mega 2560» o como un COMx.",
        )
        port_row.addWidget(self._port_box, stretch=1)

        connect = QPushButton("Conectar")
        connect.setObjectName("connect")
        connect.setProperty("state", "offline")
        connect.setIcon(icons.icon("usb", theme.ACCENT, 18))
        connect.setCursor(Qt.CursorShape.PointingHandCursor)
        connect.clicked.connect(self.connect)
        tip(connect, "Abre el puerto seleccionado (Ctrl+R).")
        self._connect_button = connect
        port_row.addWidget(connect)
        card.add_layout(port_row)

        self._device_label = QLabel("Sin dispositivo")
        self._device_label.setObjectName("value")
        tip(self._device_label, "Modelo y versión de firmware que anuncia la placa.")
        card.add(self._device_label)

        self._device_warning = QLabel("Sin sensor: la alarma no está disponible")
        self._device_warning.setStyleSheet(f"color: {theme.FAIL}; font-weight: 700;")
        self._device_warning.setWordWrap(True)
        self._device_warning.setVisible(False)
        tip(
            self._device_warning,
            "Se perdió el enlace con la placa. Mientras no vuelva, la alarma no puede "
            "detectar movimiento ni avisar.",
        )
        card.add(self._device_warning)

        self._sensor = SensorPanel()
        card.add(self._sensor, stretch=1)

        self._ready_label = QLabel("Calentamiento: —")
        self._ready_label.setObjectName("muted")
        tip(
            self._ready_label,
            "El HC-SR501 necesita hasta un minuto tras encenderse. Durante ese tiempo sus "
            "lecturas no son fiables.",
        )
        card.add(self._ready_label)

        # Cuenta atras del calentamiento. El rango es 0..1000 para tener
        # decimas de segundo sin decimales, y los segundos que faltan se leen
        # en la etiqueta de arriba: dentro de una barra de 10 px el texto se
        # recorta y no aporta nada.
        self._warmup_bar = QProgressBar()
        self._warmup_bar.setRange(0, 1000)
        self._warmup_bar.setValue(0)
        self._warmup_bar.setTextVisible(False)
        self._warmup_bar.setFixedHeight(10)
        self._warmup_bar.setVisible(False)
        tip(
            self._warmup_bar,
            "Tiempo que le queda al HC-SR501 para dar lecturas fiables tras encenderse.",
        )
        card.add(self._warmup_bar)

        counters = QHBoxLayout()
        counters.setSpacing(24)
        self._events_label = QLabel("0")
        self._events_label.setObjectName("value")
        tip(self._events_label, "Eventos de movimiento recibidos de la placa.")
        self._pulses_label = QLabel("0")
        self._pulses_label.setObjectName("value")
        tip(self._pulses_label, "Pulsos completos (inicio y fin de detección).")
        counters.addLayout(self._counter_block("Eventos", self._events_label))
        counters.addLayout(self._counter_block("Pulsos", self._pulses_label))
        counters.addStretch(1)
        card.add_layout(counters)

        row = QHBoxLayout()
        row.setSpacing(6)
        calibration = QPushButton("Ajuste del sensor…")
        calibration.setIcon(icons.icon("activity", theme.ACCENT, 18))
        calibration.clicked.connect(self.open_calibration)
        tip(calibration, "Asistente que mide el sensor y dice cómo girar los potenciómetros.")
        row.addWidget(calibration)

        skip = QPushButton("Omitir calentamiento")
        skip.setObjectName("link")
        skip.clicked.connect(self.skip_warmup)
        tip(skip, "Da el PIR por listo sin esperar el minuto de calentamiento.")
        row.addWidget(skip)
        row.addStretch(1)
        card.add_layout(row)
        return card

    @staticmethod
    def _counter_block(caption: str, value: QLabel) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(0)
        heading = QLabel(caption)
        heading.setObjectName("muted")
        heading.setFont(theme.font(8))
        column.addWidget(heading)
        column.addWidget(value)
        return column

    def _build_statusbar(self) -> None:
        bar = QStatusBar()
        self.setStatusBar(bar)
        self._status_label = QLabel("Listo.")
        bar.addWidget(self._status_label, 1)

        # Los seis canales, siempre visibles. Son indicadores: se cambian
        # desde el menu, para que la barra no sea un segundo juego de botones.
        self._channels: dict[str, QLabel] = {}
        for key, (_icon, title, where) in _CHANNELS.items():
            channel = QLabel()
            channel.setFixedSize(18, 18)
            channel.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tip(channel, f"{title} · {where}")
            bar.addPermanentWidget(channel)
            self._channels[key] = channel

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFrameShadow(QFrame.Shadow.Plain)
        divider.setFixedHeight(16)
        bar.addPermanentWidget(divider)

        self._source_label = QLabel("")
        self._source_label.setObjectName("muted")
        tip(self._source_label, "Qué ficheros de configuración se han cargado.")
        bar.addPermanentWidget(self._source_label)
        protocol = QLabel("F1 · protocolo v1")
        protocol.setObjectName("muted")
        bar.addPermanentWidget(protocol)
        self._set_source(self._config.source)
        self._paint_channels()

    def _paint_channels(self) -> None:
        """Pinta los indicadores de canal segun la configuracion vigente.

        Un canal encendido pero sin datos (correo sin credenciales, SMS sin
        cuenta) va en ambar: esta activo, pero no podria avisar. Es la
        diferencia entre «lo he desactivado» y «lo he dejado como estaba y no
        funciona», que es justo lo que se descubre el dia de la alarma.
        """
        config = self._config
        states = (
            ("voz", config.tts.enabled, True, "activa", "desactivada"),
            ("correo", config.email.enabled, config.email.configured, "activo", "desactivado"),
            ("sms", config.sms.enabled, config.sms.configured, "activo", "desactivado"),
            ("movil", config.ntfy.enabled, config.ntfy.configured, "activo", "desactivado"),
            ("zumbador", config.buzzer.enabled, True, "activo", "desactivado"),
        )
        for key, enabled, ready, on_word, off_word in states:
            icon_name, title, where = _CHANNELS[key]
            if not enabled:
                color, text = theme.OFF, f"{title}: {off_word} · {where}"
            elif not ready:
                color = theme.WARN
                text = (
                    f"{title}: {on_word}, pero faltan datos "
                    f"(revisa Avisos → Correo, SMS y móvil…) · {where}"
                )
            else:
                color, text = theme.ACCENT, f"{title}: {on_word} · {where}"
            channel = self._channels[key]
            channel.setPixmap(icons.pixmap(icon_name, color, 14))
            channel.setToolTip(text)
            channel.setAccessibleName(text)

        self._paint_remote_channel()

    def _paint_remote_channel(self) -> None:
        """El mando tiene tres estados, no dos: apagado, incompleto y escuchando.

        «Activo pero sin datos» es el estado que hay que hacer visible: el
        usuario cree que puede mandar órdenes y no llega ninguna.
        """
        channel = self._channels.get("mando")
        if channel is None:
            return
        _icon, title, where = _CHANNELS["mando"]
        remote = self._config.remote
        if not remote.enabled:
            color, text = theme.OFF, f"{title}: desactivado · {where}"
        else:
            missing = self._config.remote_missing()
            if missing:
                color = theme.WARN
                text = f"{title}: activo, pero falta {', '.join(missing)} · {where}"
            elif self._remote_listener is not None and self._remote_listener.running:
                state = self._remote_listener.snapshot()
                if state.connected:
                    color = theme.ACCENT
                    text = f"{title}: escuchando «{state.topic}» · {where}"
                else:
                    color = theme.WARN
                    text = f"{title}: sin conexión con «{state.topic}»"
                    if state.error:
                        text += f" · {state.error}"
            else:
                color = theme.WARN
                text = f"{title}: activo, pero no está escuchando · {where}"
        channel.setPixmap(icons.pixmap(_icon, color, 14))
        channel.setToolTip(text)
        channel.setAccessibleName(text)

    def _status(self, text: str) -> None:
        self._status_label.setText(text)

    def report_ui_error(self, error: BaseException) -> None:
        """Registra un fallo inesperado sin que la ventana desaparezca.

        Se llama desde el gancho de `sys.excepthook` instalado en `run()`: una
        excepcion que sale de un slot de Qt no puede abandonar el proceso (el
        gesto por defecto de PyQt6 aborta la aplicacion y la ventana se cierra
        sin explicacion). Se deja constancia en el registro y en la barra de
        estado; no se abre un modal porque el fallo puede repetirse en cada
        tic de un temporizador y bloquearia la aplicacion.
        """
        log.error(
            "Excepción no capturada en la interfaz: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        with suppress(Exception):
            self._status(f"Error inesperado: {type(error).__name__}: {error}")

    def _set_source(self, source: str) -> None:
        self._source_label.setText(f"Config: {source}")

    # ------------------------------------------------------------------
    # Estado pintado
    # ------------------------------------------------------------------

    def _state_key(self) -> str:
        if self._controller.state is AlarmState.TRIGGERED and self._controller.silenced:
            return "silenced"
        return _STATE_KEYS[self._controller.state]

    def _state_hint(self, key: str) -> str:
        """Pista del estado. Los segundos van al reloj grande, no aqui."""
        hint = _STATE_HINTS.get(key, "")
        if self._link_down:
            hint = f"{hint} {_LINK_DOWN_HINT}".strip()
        return hint

    def _update_clock(self) -> None:
        """Pinta o esconde el reloj grande del retardo en curso.

        La duracion total la da el controlador y no la configuracion: asi el
        reloj no se descuadra si el usuario cambia los retardos con la cuenta
        atras en marcha (el plazo en curso no se recorta).
        """
        remaining = self._controller.remaining_s()
        total = self._controller.delay_total_s
        if remaining is None or total <= 0:
            self._hero.clear_countdown()
            return
        color = theme.state_color(self._state_key())
        self._hero.set_countdown(remaining, total, color)

    def _paint_countdown(self) -> None:
        """Repinta pista y reloj: avanzan cada poco y no cambia nada mas."""
        self._hero.set_hint(self._state_hint(self._state_key()))
        self._update_clock()

    def _paint_alarm(self) -> None:
        key = self._state_key()
        text = self._config.ui.label(key)
        hint = self._state_hint(key)
        self._hero.set_state(key, text, hint, silenced=self._controller.silenced)
        self._update_clock()

        armed = self._controller.state is not AlarmState.DISARMED
        self._primary.setText("Desarmar" if armed else "Armar")
        self._primary.setProperty("pstate", "armed" if armed else "disarmed")
        self._primary.setIcon(icons.icon("unlock" if armed else "lock", "#ffffff", 22))
        style = self._primary.style()
        if style is not None:
            style.unpolish(self._primary)
            style.polish(self._primary)

        self._silence_button.setEnabled(self._controller.state is AlarmState.TRIGGERED)
        self._sync_actions()

    def _sync_actions(self) -> None:
        armed = self._controller.state is not AlarmState.DISARMED
        self.act_arm.setEnabled(not armed)
        self.act_disarm.setEnabled(armed)
        self.act_silence.setEnabled(self._controller.state is AlarmState.TRIGGERED)

    def _primary_action(self) -> None:
        if self._controller.state is AlarmState.DISARMED:
            self.arm()
        else:
            self.disarm()

    def _paint_sensor(self, on: bool) -> None:
        """Nivel actual del PIR en el panel del sensor."""
        self._sensor.set_level(on)

    # ------------------------------------------------------------------
    # Calentamiento del PIR
    # ------------------------------------------------------------------

    def _start_warmup(self, total_ms: int) -> None:
        """Arranca la cuenta atras con el `warmup_ms` que anuncia la placa."""
        self._warmup_total_ms = max(0, total_ms)
        self._warmup_base_ms = 0
        self._warmup_synced_at = time.monotonic()
        if not self._warmup_total_ms:
            self._ready_label.setText("Calentamiento: no requerido")
            self._warmup_bar.setVisible(False)
            return
        seconds = self._warmup_total_ms / 1000
        self._ready_label.setText(f"Calentamiento: en curso ({seconds:.0f} s)")
        self._warmup_bar.setValue(0)
        self._warmup_bar.setVisible(True)

    def _finish_warmup(self, text: str) -> None:
        """Da el calentamiento por terminado y esconde la barra."""
        self._warmup_total_ms = 0
        self._ready_label.setText(text)
        self._warmup_bar.setVisible(False)

    def _paint_warmup(self) -> None:
        """Avanza la barra extrapolando desde el ultimo `up_ms` recibido.

        No hace falta un temporizador propio: el refresco de 500 ms ya llama
        aqui, y entre latidos el reloj es mas fiable que contar mensajes.
        """
        total = self._warmup_total_ms
        if not total:
            return
        elapsed = self._warmup_base_ms + (time.monotonic() - self._warmup_synced_at) * 1000
        remaining = max(0.0, total - elapsed)
        if remaining <= 0:
            # Si el `Ready` se perdio, el `up_ms` del latido ya delata que no
            # queda calentamiento: mejor cerrar que dejar la barra llena.
            self._finish_warmup("Calentamiento: completado")
            return
        fraction = 1.0 - remaining / total
        self._warmup_bar.setValue(int(1000 * max(0.0, min(1.0, fraction))))
        self._ready_label.setText(f"Calentamiento: en curso ({remaining / 1000:.0f} s)")

    def _apply_health(self, health: Health) -> None:
        self._paint_connection(health)
        down = health is Health.OFFLINE
        if down != self._link_down:
            # Solo se repinta el hero al cambiar: hacerlo cada 500 ms reaplica
            # hojas de estilo sin que nada visible haya cambiado.
            self._link_down = down
            self._paint_alarm()
            self._device_warning.setVisible(down)

    def _paint_link_status(self, health: Health | None) -> None:
        """Linea de estado de la tarjeta: salud y antiguedad del ultimo latido."""
        manager = self._manager
        if manager is None or health is None:
            text, color = "● Sin enlace", theme.MUTED
        else:
            label, color = _HEALTH_STYLE.get(health, ("Desconocido", theme.MUTED))
            detail = ""
            if health in (Health.ONLINE, Health.DEGRADED):
                age = manager.heartbeat_age_s
                if age is not None:
                    detail = f"latido hace {age:.1f} s"
            elif manager.alive and health is Health.OFFLINE:
                remaining = manager.next_retry_in
                if remaining is not None:
                    detail = f"próximo intento en {remaining:.0f} s"
            text = f"● {label}" + (f" · {detail}" if detail else "")
        if self._link_status.text() != text:
            self._link_status.setText(text)
            self._link_status.setStyleSheet(f"color: {color}; font-weight: 600;")

    def _paint_connection(self, health: Health | None) -> None:
        """Traduce la salud del enlace al estado del boton-accion."""
        manager = self._manager
        if manager is None or health is None:
            state = "offline"
        elif health in (Health.ONLINE, Health.DEGRADED):
            state = "connected"
        elif manager.alive:
            state = "retrying"
        else:
            state = "offline"
        self._paint_connection_state(state)
        self._paint_link_status(health)
        self._sensor.set_connected(state == "connected")

    def _paint_connection_state(self, state: str) -> None:
        """Pinta el boton-accion de conexion.

        Tres estados, y cada uno dice que hacer sin leer nada mas:

        * ``connected``: verde, con check, deshabilitado. No hay nada que pulsar.
        * ``retrying``: ambar, «Reconectando…». El gestor insiste solo.
        * ``offline``: gris, «Conectar», habilitado. No hay enlace que mantener.
        """
        manager = self._manager

        if state == "connected":
            text, icon_name, icon_color, enabled = "Conectado", "check", "#ffffff", False
            age = manager.heartbeat_age_s if manager is not None else None
            detail = f"último latido hace {age:.0f} s" if age is not None else "recién abierto"
            tooltip = f"Enlace activo con {self._link_name or 'la placa'} · {detail}."
        elif state == "retrying":
            text, icon_name, icon_color, enabled = "Reconectando…", "refresh", "#ffffff", False
            remaining = manager.next_retry_in if manager is not None else None
            when = (
                f"próximo intento en {remaining:.0f} s" if remaining is not None else "reintentando"
            )
            error = manager.stats.last_error if manager is not None else ""
            tooltip = f"{when.capitalize()}." + (f"\nÚltimo error: {error}" if error else "")
        else:
            text, icon_name, icon_color, enabled = "Conectar", "usb", theme.ACCENT, True
            tooltip = "Abre el puerto seleccionado y mantiene el enlace (Ctrl+R)."

        self._connect_button.setEnabled(enabled)
        self._connect_button.setToolTip(tooltip)
        if state == self._connection_state:
            return
        self._connection_state = state
        self._connect_button.setText(text)
        self._connect_button.setIcon(icons.icon(icon_name, icon_color, 18))
        self._connect_button.setProperty("state", state)
        style = self._connect_button.style()
        if style is not None:
            style.unpolish(self._connect_button)
            style.polish(self._connect_button)

    # ------------------------------------------------------------------
    # Puertos y conexion
    # ------------------------------------------------------------------

    def _populate_ports(self) -> None:
        self._port_map.clear()
        labels: list[str] = []
        try:
            for entry in describe_ports():
                device, _, description = entry.partition(": ")
                label = f"{device}  ·  {description}" if description else device
                self._port_map[label] = device
                labels.append(label)
        except Exception:  # pragma: no cover - pyserial puede fallar al enumerar
            log.exception("no se pudieron enumerar los puertos")

        self._port_box.clear()
        self._port_box.addItems(labels)

        if self._preferred_port:
            for index, device in enumerate(self._port_map.values()):
                if device.lower() == self._preferred_port.lower():
                    self._port_box.setCurrentIndex(index)
                    return
        try:
            candidates = list_candidates()
        except Exception:  # pragma: no cover
            candidates = []
        for index, device in enumerate(self._port_map.values()):
            if device in candidates:
                self._port_box.setCurrentIndex(index)
                return

    def _refresh_ports_if_idle(self) -> None:
        """Reenumera puertos solo sin enlace, conservando la seleccion.

        Enchufar la placa despues de arrancar era un callejon sin salida: el
        desplegable se rellenaba una sola vez al inicio.
        """
        if self._manager is not None:
            return
        current = self._port_box.currentText()
        self._populate_ports()
        index = self._port_box.findText(current)
        if current and index >= 0:
            self._port_box.setCurrentIndex(index)

    def _selected_port(self) -> str | None:
        label = self._port_box.currentText()
        if not label:
            return self._preferred_port
        return self._port_map.get(label, label.split("  ")[0])

    def connect(self) -> None:
        self._expect_disconnect = False
        self.disconnect()

        if self._sim_check.isChecked():
            link: Link = SimulatedLink(
                warmup_s=5.0,
                speed=self._sim_speed,
                motion_every_s=(6.0, 14.0),
                hold_ms=2700,
            )
            self._status("Conectando al dispositivo simulado…")
        else:
            link = SerialLink(port=self._selected_port(), baudrate=self._baud)
            self._status(f"Abriendo {link.name}…")

        self._start_manager(link)
        self._log_event("info", "Enlace iniciado", link.name)

    def _start_manager(self, link: Link) -> None:
        """Crea y arranca el lector con la politica de la configuracion."""
        self._link_name = link.name
        manager = DeviceManager(
            link,
            auto_ack=True,
            hb_timeout_s=self._config.serial.heartbeat_timeout_s,
            reconnect_max_s=self._config.serial.reconnect_max_s,
            on_message=self._bridge.push_message,
            on_health=self._bridge.push_health,
        )
        self._manager = manager
        self._link_lost = False
        self._last_health = None
        manager.start()
        self._paint_connection(manager.health)

    def _restart_manager(self) -> None:
        """Relanza el hilo si ha muerto, conservando la configuracion."""
        manager = self._manager
        if manager is None:
            return
        link = manager.link
        name = self._link_name
        self._manager = None
        with suppress(Exception):
            manager.stop(timeout=1.0)
        self._log_event("info", "Enlace", f"reiniciando el lector ({name})")
        self._start_manager(link)

    def disconnect(self) -> None:
        if self._manager is None:
            return
        # La desconexion pedida por el usuario no es una averia: se marca para
        # que la transicion a OFFLINE no dispare avisos.
        self._expect_disconnect = True
        manager, self._manager = self._manager, None
        try:
            manager.stop(timeout=2.0)
        except Exception:  # pragma: no cover - cierre defensivo
            log.exception("fallo al cerrar el enlace")
        self._link_lost = False
        self._last_health = Health.UNKNOWN
        self._apply_health(Health.UNKNOWN)
        self._paint_sensor(False)
        self._sensor.set_connected(False)
        self._device_label.setText("Sin dispositivo")
        self._finish_warmup("Calentamiento: —")
        self._status("Desconectado.")

    def _on_toggle_sim(self, _checked: bool) -> None:
        self.connect()

    def skip_warmup(self) -> None:
        if self._manager is None:
            self._warn_not_connected()
            return
        self._manager.send("set", warmup_ms=0)
        self._finish_warmup("Calentamiento: omitido")
        self._status("Calentamiento omitido: el PIR se considera listo.")

    # ------------------------------------------------------------------
    # Alarma
    # ------------------------------------------------------------------

    def arm(self) -> None:
        transition = self._controller.arm()
        if transition is None:
            self._status("La alarma ya estaba armada.")
            return
        self._apply_transition(transition)

    def disarm(self) -> None:
        transition = self._controller.disarm()
        if transition is None:
            self._status("La alarma ya estaba desarmada.")
            return
        self._apply_transition(transition)

    def silence(self) -> None:
        transition = self._controller.silence()
        if transition is None:
            self._status("No hay ninguna alarma activa que silenciar.")
            return
        self._apply_transition(transition)

    def _apply_transition(self, transition: Transition, *, alert_now: bool = False) -> None:
        self._paint_alarm()
        change = (
            f"{self._config.ui.label(_STATE_KEYS[transition.previous])} → "
            f"{self._config.ui.label(self._state_key())}"
        )
        if transition.action == ACTION_TRIGGERED:
            # El disparo es la fila que alguien busca al reconstruir una
            # intrusion: va en rojo y dice cuanto va a sonar la sirena y, si
            # esta programado, cuando vuelve a armarse sola.
            detail = f"{change} · sirena {self._siren_s():g} s"
            if self._controller.rearm_enabled:
                number = self._controller.rearm_count + 1
                if number <= self._controller.rearm_max:
                    detail += (
                        f" · rearme {number} de {self._controller.rearm_max} en "
                        f"{_span(self._config.alarm.rearm_delay_s)}"
                    )
                else:
                    detail += " · sin rearmes disponibles"
            self._log_event("triggered", "Alarma disparada", detail)
        elif transition.action == ACTION_REARMING:
            number = self._controller.rearm_count + 1
            self._log_event(
                "rearming",
                "Sirena terminada",
                f"{change} · rearme en {_span(self._config.alarm.rearm_delay_s)} "
                f"({number} de {self._controller.rearm_max})",
            )
        elif transition.action == ACTION_REARMED:
            self._log_event(
                "rearmed",
                "Rearmada tras disparo",
                f"{change} · rearme {self._controller.rearm_count} de {self._controller.rearm_max}",
            )
        elif transition.action == ACTION_REARM_EXHAUSTED:
            self._log_event(
                "error",
                "Rearme automático agotado",
                f"Rearmes usados: {self._controller.rearm_max} de "
                f"{self._controller.rearm_max}. La alarma no volverá a armarse sola: "
                "desarma a mano.",
            )
        else:
            self._log_event("alarm", "Alarma", change)

        # El estado del dispositivo se sincroniza siempre, tambien al silenciar:
        # asi el zumbador local deja de pitar aunque el USB vaya con retraso.
        self._sync_device_armed()

        hour = time.strftime("%H:%M:%S")
        if transition.action == ACTION_ARMING:
            # Retardo de salida: el aviso lo marca la placa y la alarma todavia
            # no vigila, asi que salir por delante del PIR no dispara nada.
            self._device_stop_beep()
            self._device_beep("exit", self._config.alarm.exit_delay_s * 1000, force=True)
            self._status(
                f"Armando: sal de casa. La alarma se activará en "
                f"{self._config.alarm.exit_delay_s} s."
            )
        elif transition.action == ACTION_PENDING:
            # Retardo de entrada: suena distinto que el de salida para que se
            # reconozca de oido, y da tiempo a desarmar.
            self._device_stop_beep()
            self._device_beep("entry", self._config.alarm.entry_delay_s * 1000, force=True)
            self._status("Presencia detectada: desarma antes de que se dispare la alarma.")
        elif transition.action == ACTION_ARMED:
            self._device_stop_beep()
            self._announce("armed", hour, "Alarma armada")
            self._device_beep("armed")
            self._notify("armed", "Alarma armada", "La alarma ha quedado armada.")
        elif transition.action == ACTION_DISARMED:
            self._device_stop_beep()
            self._cancel_pending_voice("triggered")
            self._announce("disarmed", hour, "Alarma desarmada")
            self._device_beep("disarmed")
            self._notify("disarmed", "Alarma desarmada", "La alarma ha quedado desarmada.")
        elif transition.action == ACTION_TRIGGERED:
            self._device_stop_beep()
            self._cancel_pending_voice("armed", "disarmed")
            self._announce("triggered", hour, "Presencia detectada")
            self._device_beep("alarm", self._config.buzzer.siren_ms)
            self._notify(
                "triggered",
                "Presencia detectada",
                "Se ha detectado movimiento con la alarma armada.",
                # El pánico lo pide una persona: el enfriamiento no puede
                # callar el unico aviso que alguien esta esperando.
                ignore_gate=alert_now,
            )
        elif transition.action == ACTION_SILENCED:
            self._speech.stop()
            self._device_stop_beep()
            self._status("Alarma silenciada: sigue armada y vigilando.")
        elif transition.action == ACTION_REARMING:
            # La sirena ya callo. Se reutiliza el reloj grande del hero para
            # que la espera se vea igual que los retardos de entrada y salida.
            self._device_stop_beep()
            number = self._controller.rearm_count + 1
            self._status(
                f"Sirena terminada. La alarma se rearmará sola en "
                f"{_span(self._config.alarm.rearm_delay_s)} ({number} de "
                f"{self._controller.rearm_max})."
            )
        elif transition.action == ACTION_REARMED:
            self._device_stop_beep()
            self._announce(
                "rearmed",
                hour,
                "Alarma rearmada",
                n=self._controller.rearm_count,
                max=self._controller.rearm_max,
            )
            self._device_beep("armed")
            self._notify(
                "armed",
                "Alarma rearmada tras un disparo",
                f"Rearme {self._controller.rearm_count} de "
                f"{self._controller.rearm_max}. La alarma vuelve a vigilar.",
            )
            self._status(
                f"Rearmada tras el disparo ({self._controller.rearm_count} de "
                f"{self._controller.rearm_max}): vuelve a vigilar."
            )
        elif transition.action == ACTION_REARM_EXHAUSTED:
            self._status(
                "Rearme automático agotado: la alarma queda disparada hasta que la desarmes."
            )

    def _cancel_pending_voice(self, *keys: str) -> None:
        """Anula avisos que aun no han sonado y ya no vienen a cuento."""
        cancelled = self._speech.cancel_pending(tuple(keys))
        if cancelled:
            log.debug("anulados %d avisos de voz", cancelled)

    def _announce(self, key: str, hour: str, event: str, **extra: object) -> None:
        values = {name: str(value) for name, value in extra.items()}
        text = self._config.tts.text(
            key, hora=hour, evento=event, zona="zona 1", duracion="", **values
        )
        if not self._config.tts.event(key).log:
            # La locucion se dice igual, pero el usuario ha pedido no llenar el
            # registro con ella. El cambio de estado se apunta aparte.
            self._speech.say_event(key, text)
            return
        if self._speech.say_event(key, text):
            self._log_event("info", "Voz", text)
        else:
            self._log_event("info", "Voz", "desactivada; no se reproduce el aviso")

    def _sync_device_armed(self) -> None:
        manager = self._manager
        if manager is None:
            return
        try:
            manager.send(
                "set",
                armed=1 if self._controller.armed else 0,
                buzzer=1 if self._config.buzzer.enabled else 0,
                siren_ms=self._config.buzzer.siren_ms,
            )
        except Exception:  # noqa: BLE001 - el enlace puede estar cayendo
            log.debug("no se pudo sincronizar el estado de alarma", exc_info=True)

    def _resume_delay_beeps(self) -> None:
        """Reanuda la cuenta atras en una placa que acaba de reiniciarse.

        Al abrir el puerto el Arduino pierde el tono en curso. Si el retardo
        sigue vivo, se le manda solo lo que queda, no la duracion completa:
        repetir la cadencia entera alargaria el aviso mas que el plazo.
        """
        kind = self._controller.delay_kind
        remaining = self._controller.remaining_s()
        if kind is None or remaining is None:
            return
        self._device_beep(kind, max(0, int(remaining * 1000)), force=True)

    def _resume_siren(self) -> None:
        """Vuelve a pedir la sirena si el disparo sigue activo.

        Abrir el puerto reinicia la placa y esta pierde el patron en curso. Con
        la alarma disparada y sin silenciar, el aviso tiene que seguir sonando:
        un tiron del USB no puede dejar la sirena en silencio. Se fuerza porque
        la placa recien arrancada aun esta en el silencio del calentamiento, y
        aqui no es una prueba: la alarma ya estaba disparada.
        """
        if self._controller.state is not AlarmState.TRIGGERED:
            return
        if self._controller.silenced:
            return
        self._device_beep("alarm", self._config.buzzer.siren_ms, force=True)
        # La placa vuelve a pitar el patron completo: el plazo del rearme se
        # reinicia con ella, para que la espera no empiece mientras suena.
        self._controller.restart_siren()

    def _send_beep(self, name: str, ms: int = 0, *, force: bool = False, **extra: object) -> bool:
        """Envia un pitido sin mirar la configuracion local.

        `force` salta el silencio del calentamiento en el firmware: es lo que
        permite comprobar el cableado nada mas conectar la placa. Devuelve si
        la orden llego a encolarse, para que la ventana pueda confirmarlo.
        """
        manager = self._manager
        if manager is None:
            return False
        try:
            manager.send("beep", name=name, ms=ms, force=1 if force else 0, **extra)
        except Exception:  # noqa: BLE001
            log.debug("no se pudo enviar el pitido %s", name, exc_info=True)
            return False
        return True

    def _device_beep(self, name: str, ms: int = 0, *, force: bool = False) -> None:
        if not self._config.buzzer.enabled:
            return
        self._send_beep(name, ms, force=force)

    def _siren_s(self) -> float:
        """Duracion configurada de la sirena, en segundos, para leerla."""
        return round(self._config.buzzer.siren_ms / 1000, 1)

    def _device_stop_beep(self) -> None:
        """Corta el patron en curso, sea sirena o cuenta atras."""
        manager = self._manager
        if manager is None:
            return
        try:
            manager.send("beep", name="stop")
        except Exception:  # noqa: BLE001
            log.debug("no se pudo detener el zumbador", exc_info=True)

    # ------------------------------------------------------------------
    # Avisos remotos
    # ------------------------------------------------------------------

    def _notify(self, cause: str, event: str, detail: str, *, ignore_gate: bool = False) -> None:
        hour = time.strftime("%H:%M:%S")
        body = (
            f"{event}.\n\nHora: {hour}\nEstado: {self._config.ui.label(self._state_key())}\n"
            f"{detail}\n"
        )
        subject = self._config.email.subject.format(evento=event, hora=hour)

        channels = self._notifier.channels_for(cause)
        if not channels:
            self._log_event(
                "info", "Aviso", "correo/SMS/móvil desactivado, sin configurar o sin evento"
            )
            return
        if self._notifier.submit(cause, subject, body, ignore_gate=ignore_gate):
            self._log_event("info", "Aviso", f"{', '.join(channels)} en cola: {event}")
        else:
            self._log_event("bounce", "Aviso", f"«{event}» bloqueado por enfriamiento o cola llena")

    def _on_notify_result(self, result: DeliveryResult, cause: str) -> None:
        """Se ejecuta en el hilo del notificador: solo reemite."""
        self._bridge.push_result(result, cause)

    def _on_notify_result_main(self, result: DeliveryResult, cause: str) -> None:
        tag = "info" if result.ok else "error"
        label = _CHANNEL_RESULT_LABELS.get(result.channel, result.channel)
        self._log_event(tag, f"Aviso {label}", f"{cause}: {result.status} · {result.detail}")

    # ------------------------------------------------------------------
    # Mando remoto por ntfy
    # ------------------------------------------------------------------

    #: Ayuda que responde a «ayuda». Nunca dice la clave ni el código.
    _REMOTE_HELP = (
        "Órdenes disponibles (la clave o el código TOTP va delante o detrás):\n"
        "· armar / activar\n"
        "· desarmar / apagar\n"
        "· consultar / estado\n"
        "· silenciar / callar\n"
        "· ayuda\n\n"
        "Ejemplo: «clave armar» o «123456 consultar».\n"
        "El móvil debe estar suscrito al tema de mando, no al de avisos."
    )

    def open_remote(self) -> None:
        dialog = RemoteDialog(
            self,
            config=self._config,
            on_save=self._save_settings,
            snapshot=self._remote_state,
            on_send_status=self._send_remote_status,
        )
        dialog.exec()
        # Al cerrar sin guardar tambien se repinta: el indicador no debe quedar
        # mintiendo si el listener cambio mientras el dialogo estaba abierto.
        self._paint_remote_channel()

    def _remote_state(self) -> RemoteState:
        """Fotografia para el dialogo, tambien cuando no hay listener."""
        listener = self._remote_listener
        if listener is not None:
            return listener.snapshot()
        return RemoteState(
            listening=False,
            connected=False,
            since=time.time(),
            topic=self._config.remote_topic,
            error=(
                "mando desactivado"
                if not self._config.remote.enabled
                else "faltan datos: " + ", ".join(self._config.remote_missing())
            ),
        )

    def _sync_remote_listener(self) -> None:
        """Crea, mantiene o retira el listener segun la configuracion vigente.

        Se compara la configuracion completa y no solo el tema: cambiar la clave
        o el TOTP tiene que reiniciar el listener o seguiria aceptando la
        credencial anterior.
        """
        config = self._config
        wanted = config.remote.enabled and not config.remote_missing()
        current = self._remote_listener
        if current is not None and (
            not wanted or not current.matches(config.ntfy, config.remote, config.remote_topic)
        ):
            current.stop()
            self._remote_listener = None
            self._log_event("info", "Mando remoto", "escucha detenida")
        if self._remote_listener is None and wanted:
            listener = CommandListener(
                config.ntfy,
                config.remote,
                config.remote_topic,
                on_command=self._bridge.push_remote_command,
                on_state=self._bridge.push_remote_state,
                on_event=self._bridge.push_remote_log,
            )
            listener.start()
            self._remote_listener = listener
            self._log_event(
                "info", "Mando remoto", f"escuchando órdenes en «{config.remote_topic}»"
            )
        self._paint_remote_channel()

    def _on_remote_state(self, state: object) -> None:
        """Llega del hilo del listener; Qt la entrega en el hilo de la ventana."""
        if isinstance(state, RemoteState):
            self._paint_remote_channel()

    def _on_remote_log(self, kind: str, title: str, detail: str) -> None:
        self._log_event(kind or "info", f"Mando remoto: {title}", detail)

    def _status_info(self) -> StatusInfo:
        """Datos para responder a «consultar» con lo que la ventana ya sabe."""
        controller = self._controller
        key = self._state_key()
        label = self._config.ui.label(key)
        if controller.silenced:
            label = f"{label} (silenciada)"
        link = self._link_name or "sin enlace"
        if self._manager is not None:
            health = self._manager.health
            link = f"{link} · {_HEALTH_STYLE.get(health, ('', ''))[0]}"
        sensor = ""
        if self._warmup_total_ms:
            elapsed = time.monotonic() - self._warmup_synced_at
            left = max(0.0, self._warmup_total_ms / 1000 - elapsed)
            sensor = f"calentando, {left:.0f} s" if left > 0 else "listo"
        channels = tuple(
            f"{_CHANNEL_RESULT_LABELS.get(channel, channel)}"
            for channel in self._notifier.channels_for("triggered")
        )
        try:
            events = tuple(
                f"{row.local_time()[11:16]} · {row.kind}: {row.detail}"
                for row in self._store.recent(3)
            )
        except Exception:  # noqa: BLE001 - el historial no puede tumbar una respuesta
            events = ()
        return StatusInfo(
            state_label=label,
            link=link,
            sensor=sensor,
            armed=controller.armed,
            silenced=controller.silenced,
            siren_s=controller.siren_remaining_s(),
            closing_s=controller.remaining_s(),
            delay_kind=controller.delay_kind,
            delays=(
                f"salida {_delay_text(self._config.alarm.exit_delay_s)}"
                f" · entrada {_delay_text(self._config.alarm.entry_delay_s)}"
            ),
            channels=channels,
            events=events,
            now=time.strftime("%H:%M:%S"),
        )

    def _on_remote_command(self, command: object) -> None:
        """Ejecuta una orden del movil en el hilo de Qt.

        Es la unica parte que toca el controlador: el listener solo emitio una
        senal. Se responde con el resultado real, no con un «hecho» optimista.
        """
        if not isinstance(command, RemoteCommand):
            return
        listener = self._remote_listener
        action = command.action

        if action == "status":
            text = format_status(self._status_info())
            self._log_event("info", "Mando remoto", "consulta de estado desde el móvil")
            self._reply_remote(text, title="[Mando] Estado")
            return
        if action == "help":
            self._log_event("info", "Mando remoto", "ayuda solicitada desde el móvil")
            self._reply_remote(self._REMOTE_HELP, title="[Mando] Ayuda")
            return
        if action not in {"arm", "disarm", "silence", "panic"}:
            return
        if not self._config.remote.allows_action(action):
            # El listener ya filtra, pero una orden no debe ejecutarse por el
            # hecho de haber llegado: la configuracion vigente manda.
            self._log_event("error", "Mando remoto", f"{command.label}: orden desactivada")
            self._reply_remote(
                f"La orden «{command.label}» está desactivada en este equipo.",
                title="[Mando] No se pudo",
                ok=False,
            )
            return

        try:
            transition, text = self._run_remote_action(action)
        except Exception as exc:  # noqa: BLE001 - se contesta con el fallo
            log.exception("fallo al ejecutar una orden remota")
            self._log_event("error", "Mando remoto", f"{command.label}: {exc}")
            self._reply_remote(
                f"No se pudo ejecutar «{command.label}».\n\nError: {exc}",
                title="[Mando] No se pudo",
                ok=False,
            )
            return

        tag = "alarm" if transition is not None else "info"
        self._log_event(tag, "Mando remoto", f"{command.label}: {text}")
        title = f"[Mando] {command.label}"
        if transition is None:
            title = f"{title} (sin cambios)"
        self._reply_remote(text, title=title)
        if listener is not None and not listener.running:
            self._status("El mando remoto no está escuchando: revisa Avisos → Mando remoto…")

    def _run_remote_action(self, action: str) -> tuple[Transition | None, str]:
        """Ejecuta la orden y devuelve la transicion y la respuesta."""
        controller = self._controller
        if action == "arm":
            transition = controller.arm()
            if transition is None:
                return None, "No se hizo nada: la alarma ya estaba armada o armándose."
            self._apply_transition(transition)
            if transition.action == ACTION_ARMING:
                return (
                    transition,
                    f"Armada. Vigila en {self._config.alarm.exit_delay_s} s (retardo de salida).",
                )
            return transition, "Armada. Vigilando."

        if action == "disarm":
            transition = controller.disarm()
            if transition is None:
                return None, "No se hizo nada: la alarma ya estaba desarmada."
            self._apply_transition(transition)
            return transition, "Desarmada. La vigilancia está apagada."

        if action == "silence":
            transition = controller.silence()
            if transition is None:
                if controller.state is AlarmState.TRIGGERED:
                    return None, "No se hizo nada: la sirena ya estaba silenciada."
                return None, "No se hizo nada: no hay ninguna sirena sonando."
            self._apply_transition(transition)
            return transition, "Silenciada. La alarma sigue armada y vigilando."

        transition = controller.panic()
        if transition is None:
            return None, "No se hizo nada: la alarma ya estaba disparada."
        # El aviso de pánico se salta el enfriamiento: lo ha pedido una persona.
        self._apply_transition(transition, alert_now=True)
        return (
            transition,
            f"Pánico activado: sirena durante {self._siren_s():g} s. "
            "Desarma o silencia para pararla.",
        )

    def _reply_remote(self, text: str, *, title: str, ok: bool = True) -> None:
        listener = self._remote_listener
        if listener is None:
            return
        tags = ("mando-reply", "white_check_mark" if ok else "x")
        priority = "default" if ok else "high"
        listener.reply(text, title=title, priority=priority, tags=tags)

    def _send_remote_status(self) -> None:
        """Botón de prueba del diálogo: publica el estado ahora mismo."""
        listener = self._remote_listener
        if listener is None:
            QMessageBox.warning(
                self,
                "Mando remoto",
                "El mando no está escuchando. Actívalo, guarda y vuelve a probar: el envío "
                "usa el mismo tema y el mismo token que la escucha.",
            )
            return
        listener.reply(format_status(self._status_info()), title="[Mando] Estado")
        self._status("Estado enviado al móvil.")

    def test_email(self) -> None:
        self._test_channel("correo", self._config)

    def test_sms(self) -> None:
        self._test_channel("sms", self._config)

    def test_ntfy(self) -> None:
        self._test_channel("móvil", self._config)

    def _test_channel(self, kind: str, config: AppConfig, *, retry: bool = False) -> None:
        """Prueba de envio en un hilo aparte; el resultado vuelve por senal."""
        hour = time.strftime("%H:%M:%S")
        self._pending_test[kind] = config

        def work() -> DeliveryResult:
            channel: Channel
            if kind == "correo":
                channel = EmailChannel(replace(config.email, enabled=True))
                subject = config.email.subject.format(evento="Prueba", hora=hour)
                body = (
                    "Mensaje de prueba de la alarma de presencia.\n\n"
                    "Si lo lees, el envío funciona.\n"
                )
            elif kind == "sms":
                channel = build_sms_channel(replace(config.sms, enabled=True), self._email)
                subject = config.sms.template.format(evento="Prueba", hora=hour)
                body = subject
            else:
                channel = NtfyChannel(replace(config.ntfy, enabled=True))
                subject = config.ntfy.title.format(evento="Prueba", hora=hour)
                body = (
                    "Mensaje de prueba de la alarma de presencia.\n\n"
                    "Si lo ves en el móvil, el aviso por ntfy funciona."
                )
            return channel.send(subject, body)

        def worker() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - se informa en la ventana
                name = "email" if kind == "correo" else ("sms" if kind == "sms" else "ntfy")
                result = DeliveryResult(name, False, str(exc))
            self._bridge.test_finished.emit(kind, result)

        self._status(f"Enviando prueba de {kind}…")
        self._log_event("info", "Prueba", f"{kind}: envío en curso")
        threading.Thread(target=worker, name=f"test-{kind}", daemon=True).start()

    def _on_test_finished(self, kind: str, result: DeliveryResult) -> None:
        tag = "info" if result.ok else "error"
        self._log_event(tag, f"Prueba {kind}", f"{result.status} · {result.detail}")
        self._status(f"Prueba de {kind}: {result.status}.")
        config = self._pending_test.pop(kind, self._config)
        ResultDialog(
            self,
            title=f"Prueba de {kind}",
            ok=result.ok,
            detail=result.detail,
            on_retry=lambda: self._test_channel(kind, config, retry=True),
        ).exec()

    # ------------------------------------------------------------------
    # Dialogos
    # ------------------------------------------------------------------

    def open_voice_config(self) -> None:
        """Una sola ventana para todos los avisos hablados y la voz del sistema."""
        dialog = VoiceConfigDialog(
            self,
            config=self._config,
            on_save=self._save_settings,
            on_try=self._try_voice,
        )
        dialog.exec()

    def _try_voice(self, text: str, repeat: int, delay_ms: int, gap_ms: int = 0) -> None:
        """Prueba desde la tabla de voces. Devuelve nada: solo encola."""
        self._speech.say_event("generic", text, repeat=repeat, delay_ms=delay_ms, gap_ms=gap_ms)

    def open_notifications(self) -> None:
        dialog = NotificationsDialog(
            self,
            config=self._config,
            on_save=self._save_settings,
            on_test_email=lambda config: self._test_channel("correo", config),
            on_test_sms=lambda config: self._test_channel("sms", config),
            on_test_ntfy=lambda config: self._test_channel("móvil", config),
        )
        dialog.exec()

    def open_about(self) -> None:
        AboutDialog(self, simulated=self._sim_check.isChecked()).exec()

    def open_calibration(self) -> None:
        if self._manager is None:
            self._warn_not_connected()
            return
        if self._calib_dialog is not None:
            self._calib_dialog.raise_()
            self._calib_dialog.activateWindow()
            return

        set_enabled(self._config.ui.beeps)
        beep_async("quiet")
        self._calib_dialog = CalibrationDialog(
            self,
            manager=self._manager,
            bridge=self._bridge,
            on_close=self._on_calibration_closed,
        )
        self._calib_dialog.show()
        self._status("Modo de ajuste abierto.")

    def _on_calibration_closed(self) -> None:
        self._calib_dialog = None
        self._status("Modo de ajuste cerrado.")

    # ------------------------------------------------------------------
    # Configuracion
    # ------------------------------------------------------------------

    def _save_settings(self, config: AppConfig, secrets: dict[str, str] | None = None) -> None:
        """Escribe la capa local y aplica en caliente. Puede lanzar OSError."""
        if secrets:
            update_env(self._env_path, secrets)
        save_local(config, self._local_path)
        self._apply_config(config)
        self._status("Configuración guardada y aplicada.")

    def _persist(self, config: AppConfig, *, message: str) -> bool:
        """Guarda en la capa local lo cambiado desde un menu.

        Los checks de canal se tocaban solo en memoria: al recargar o reabrir
        volvian a su valor del fichero. Si la escritura falla se avisa y no se
        aplica nada, para que el menu no diga algo distinto de lo guardado.
        """
        try:
            save_local(config, self._local_path)
        except Exception as exc:  # noqa: BLE001 - se informa al usuario
            log.exception("no se pudo guardar la configuracion")
            QMessageBox.critical(
                self, "No se pudo guardar", f"La configuración no se ha guardado:\n{exc}"
            )
            return False
        self._apply_config(config)
        self._status(message)
        return True

    def _confirm_disable(self, title: str, question: str) -> bool:
        """Pide confirmacion antes de apagar un canal de aviso.

        Apagar un canal es silencioso: no avisa de nada, y el fallo se descubre
        el dia que la alarma suena y nadie se entera. Se pregunta antes y la
        respuesta por defecto es no.
        """
        answer = QMessageBox.question(
            self,
            title,
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer is QMessageBox.StandardButton.Yes

    def _apply_config(self, config: AppConfig) -> None:
        # `load_config` ya calcula el origen real; solo se rellena cuando viene
        # de los valores por defecto y nadie lo ha fijado aun.
        source = config.source
        if source == "valores por defecto":
            source = f"{self._config_path} + {self._local_path}"
        self._config = replace(config, source=source)
        self._speech.reconfigure(self._config.tts)
        self._email.reconfigure(self._config.email)
        self._sms.reconfigure(self._config.sms)
        self._ntfy.reconfigure(self._config.ntfy)
        self._notifier.reconfigure([self._email, self._sms, self._ntfy])
        self._controller.set_limits(
            cooldown_s=self._config.alarm.cooldown_s, max_per_hour=self._config.alarm.max_per_hour
        )
        self._controller.set_delays(
            exit_delay_s=self._config.alarm.exit_delay_s,
            entry_delay_s=self._config.alarm.entry_delay_s,
        )
        self._controller.set_rearm(
            auto_rearm=self._config.alarm.auto_rearm,
            rearm_delay_s=self._config.alarm.rearm_delay_s,
            max_rearms=self._config.alarm.max_rearms,
        )
        self._controller.set_siren_s(self._config.buzzer.siren_ms / 1000)
        # Cuantas filas caben en el registro es configuracion, no estado de la
        # sesion: una copia restaurada que cambie `log.visible_rows` debe
        # aplicarse sin reiniciar.
        self._log.set_limit(self._config.log.visible_rows)
        for action, value in (
            (self.act_tts, self._config.tts.enabled),
            (self.act_email, self._config.email.enabled),
            (self.act_sms, self._config.sms.enabled),
            (self.act_ntfy, self._config.ntfy.enabled),
            (self.act_show_heartbeats, self._config.ui.show_heartbeats),
            (self.act_beeps, self._config.ui.beeps),
        ):
            blocked = action.blockSignals(True)
            action.setChecked(value)
            action.blockSignals(blocked)
        set_enabled(self._config.ui.beeps)
        self._paint_alarm()
        self._paint_channels()
        self._set_source(self._config.source)
        # Un cambio guardado (sirena, zumbador) se aplica en el acto a la placa.
        self._sync_device_armed()
        # Y el mando remoto se reinicia solo si cambio algo que le afecte.
        self._sync_remote_listener()

    def reload_config(self) -> None:
        try:
            config = load_config(
                self._config_path, env=load_env(self._env_path), local=self._local_path
            )
        except ConfigError as exc:
            QMessageBox.critical(self, "Configuración inválida", str(exc))
            self._log_event("error", "Configuración", str(exc))
            return
        # `_apply_config` ya reenvia el estado a la placa.
        self._apply_config(config)
        self._log_event("info", "Configuración", f"recargada desde {config.source}")
        self._status("Configuración recargada.")

    def _on_toggle_tts(self, checked: bool) -> None:
        if not checked and not self._confirm_disable(
            "Desactivar la voz",
            "Si desactivas la voz, la alarma dejará de anunciarse por el altavoz.\n\n"
            "El correo, el SMS, el móvil y el zumbador de la placa no cambian. ¿Continuar?",
        ):
            self._restore_check(self.act_tts, True)
            self._status("La voz sigue activada.")
            return
        config = replace(self._config, tts=replace(self._config.tts, enabled=checked))
        if self._persist(config, message="Voz activada." if checked else "Voz desactivada."):
            return
        self._restore_check(self.act_tts, not checked)

    def _on_toggle_email(self, checked: bool) -> None:
        if not checked and not self._confirm_disable(
            "Desactivar el correo",
            "Si desactivas el correo, la alarma no enviará avisos por correo.\n\n"
            "La voz, el SMS, el móvil y el zumbador de la placa no cambian. ¿Continuar?",
        ):
            self._restore_check(self.act_email, True)
            self._status("El correo sigue activado.")
            return
        config = replace(self._config, email=replace(self._config.email, enabled=checked))
        if self._persist(config, message="Correo activado." if checked else "Correo desactivado."):
            return
        self._restore_check(self.act_email, not checked)

    def _on_toggle_sms(self, checked: bool) -> None:
        if not checked and not self._confirm_disable(
            "Desactivar los SMS",
            "Si desactivas los SMS, la alarma no enviará avisos por mensaje de texto.\n\n"
            "La voz, el correo, el móvil y el zumbador de la placa no cambian. ¿Continuar?",
        ):
            self._restore_check(self.act_sms, True)
            self._status("Los SMS siguen activados.")
            return
        config = replace(self._config, sms=replace(self._config.sms, enabled=checked))
        if self._persist(config, message="SMS activados." if checked else "SMS desactivados."):
            return
        self._restore_check(self.act_sms, not checked)

    def _on_toggle_ntfy(self, checked: bool) -> None:
        if not checked and not self._confirm_disable(
            "Desactivar el aviso al móvil",
            "Si desactivas ntfy, la alarma no enviará notificaciones al teléfono.\n\n"
            "La voz, el correo, el SMS y el zumbador de la placa no cambian. ¿Continuar?",
        ):
            self._restore_check(self.act_ntfy, True)
            self._status("El aviso al móvil sigue activado.")
            return
        config = replace(self._config, ntfy=replace(self._config.ntfy, enabled=checked))
        if self._persist(
            config, message="Aviso al móvil activado." if checked else "Aviso al móvil desactivado."
        ):
            return
        self._restore_check(self.act_ntfy, not checked)

    def _on_toggle_heartbeats(self, checked: bool) -> None:
        config = replace(self._config, ui=replace(self._config.ui, show_heartbeats=checked))
        if self._persist(
            config,
            message="Latidos visibles." if checked else "Latidos ocultos.",
        ):
            return
        self._restore_check(self.act_show_heartbeats, not checked)

    def _on_toggle_beeps(self, checked: bool) -> None:
        config = replace(self._config, ui=replace(self._config.ui, beeps=checked))
        if self._persist(
            config, message="Pitidos de ajuste activados." if checked else "Pitidos silenciados."
        ):
            return
        self._restore_check(self.act_beeps, not checked)

    def _restore_check(self, action: QAction, value: bool) -> None:
        """Devuelve el check a su valor si no se pudo guardar el cambio."""
        blocked = action.blockSignals(True)
        action.setChecked(value)
        action.blockSignals(blocked)

    def test_voice(self) -> None:
        if not self._speech.enabled:
            self._status("La voz está desactivada en el menú Avisos.")
            return
        text = self._config.tts.text("armed", hora=time.strftime("%H:%M:%S"), evento="Prueba")
        self._speech.say_event("armed", text)
        self._status("Prueba de voz encolada.")
        self._log_event("info", "Voz", f"prueba: {text}")

    def test_buzzer(self) -> None:
        if not self._config.buzzer.enabled:
            self._status("El zumbador está desactivado en la configuración.")
            return
        if self._manager is None:
            self._warn_not_connected()
            return
        # `force`: la prueba manual suena aunque el PIR siga calentando.
        self._device_beep("test", force=True)
        self._log_event("info", "Zumbador", "patrón de prueba enviado")

    def open_buzzer_config(self) -> None:
        """Prueba de cableado y ajuste fino del zumbador."""
        dialog = BuzzerDialog(
            self,
            config=self._config,
            on_save=self._save_settings,
            on_pattern=self._play_buzzer_pattern,
            on_tone=self._play_buzzer_tone,
            connected=self._manager is not None,
            warming=self._warmup_total_ms > 0,
            device_pin=self._device_buzzer_pin,
        )
        dialog.exec()

    def open_delays_config(self) -> None:
        """Ajuste de los retardos de entrada y salida, con prueba de cadencia."""
        dialog = DelaysDialog(
            self,
            config=self._config,
            on_save=self._save_settings,
            on_preview=self._play_delay_preview,
            connected=self._manager is not None,
        )
        dialog.exec()

    def _play_delay_preview(self, kind: str, ms: int) -> bool:
        """Escucha una cadencia sin armar nada ni esperar los segundos reales."""
        if self._manager is None:
            self._warn_not_connected()
            return False
        if not self._config.buzzer.enabled:
            self._status("El zumbador está desactivado en la configuración.")
            return False
        if self._send_beep(kind, ms, force=True):
            self._log_event("info", "Zumbador", f"prueba manual: retardo de {kind}")
            return True
        return False

    def _play_buzzer_pattern(self, name: str, ms: int, enabled: bool) -> bool:
        """Reproduce un patrón desde el diálogo, antes de guardar el ajuste."""
        if self._manager is None:
            self._warn_not_connected()
            return False
        if not enabled:
            self._status("Activa «Pitidos locales en la placa» para poder probar.")
            return False
        # La casilla puede estar recién marcada y aún sin guardar: la placa
        # necesita su `buzzer=1` para obedecer el patrón.
        if not self._config.buzzer.enabled:
            self._sync_buzzer_flag(True)
        if self._send_beep(name, ms, force=True):
            self._log_event("info", "Zumbador", f"prueba manual: patrón {name}")
            return True
        return False

    def _play_buzzer_tone(self, hz: int, ms: int, enabled: bool) -> bool:
        if self._manager is None:
            self._warn_not_connected()
            return False
        if not enabled:
            self._status("Activa «Pitidos locales en la placa» para poder probar.")
            return False
        if not self._config.buzzer.enabled:
            self._sync_buzzer_flag(True)
        if self._send_beep("tone", ms, force=True, freq=hz):
            self._log_event("info", "Zumbador", f"tono de prueba {hz} Hz durante {ms} ms")
            return True
        return False

    def _sync_buzzer_flag(self, enabled: bool) -> None:
        """Adelanta el interruptor del zumbador a la placa sin tocar el fichero."""
        manager = self._manager
        if manager is None:
            return
        try:
            manager.send("set", buzzer=1 if enabled else 0)
        except Exception:  # noqa: BLE001
            log.debug("no se pudo adelantar el interruptor del zumbador", exc_info=True)

    # ------------------------------------------------------------------
    # Mensajes del dispositivo
    # ------------------------------------------------------------------

    def _on_message(self, message: Message) -> None:
        if isinstance(message, Hello):
            zones = ", ".join(f"D{z.pin} ({z.name})" for z in message.zones)
            self._device_label.setText(f"{message.dev} · fw {message.fw}")
            self._log_event("info", "Dispositivo", f"{message.dev} fw {message.fw} · {zones}")
            self._device_buzzer_pin = _as_int(message.cfg.get("buzzer_pin"))
            self._start_warmup(int(message.cfg.get("warmup_ms", 0)))
            # Abrir el puerto reinicia la placa por DTR: su RAM vuelve a
            # `armed=0` y `buzzer=1`. Se reenvia el estado sin pitar.
            self._sync_device_armed()
            self._resume_delay_beeps()
            self._resume_siren()

        elif isinstance(message, Ready):
            seconds = message.warmup_ms / 1000
            self._finish_warmup(f"Calentamiento: completado ({seconds:.0f} s)")
            self._log_event("info", "PIR listo", "Calentamiento terminado")
            self._status("Sensor operativo.")
            if self._config.tts.event("device_ready").enabled:
                text = self._config.tts.text("device_ready")
                self._speech.say_event("device_ready", text)

        elif isinstance(message, Heartbeat):
            self._level = message.level
            self._sensor.beat()
            self._paint_sensor(message.level)
            # El latido trae `up_ms`: sirve para resincronizar la cuenta atras
            # aunque el primero llegue a mitad del calentamiento.
            self._warmup_base_ms = message.up_ms
            self._warmup_synced_at = time.monotonic()
            if message.warming_up or self._warmup_total_ms:
                self._paint_warmup()
            if message.lost:
                self._status(
                    f"El dispositivo descartó {message.lost} evento(s) por desbordamiento."
                )
            if self.act_show_heartbeats.isChecked():
                self._log_event(
                    "heartbeat",
                    "Latido",
                    f"t={message.up_ms / 1000:.1f} s · nivel={int(message.level)} · "
                    f"buffer={message.buffered} · perdidos={message.lost}",
                )

        elif isinstance(message, MotionEvent):
            self._events += 1
            self._events_label.setText(str(self._events))
            if message.kind is MotionKind.START:
                self._pulses += 1
                self._pulses_label.setText(str(self._pulses))
                self._paint_sensor(True)
                self._log_event("start", "Movimiento", "Detección iniciada")
                transition = self._controller.feed(message)
                if transition is not None:
                    self._apply_transition(transition)
            elif message.kind is MotionKind.END:
                self._paint_sensor(False)
                duration = f"{message.dur_ms} ms" if message.dur_ms else "—"
                self._log_event("end", "Fin de pulso", f"duración {duration}")
            else:
                self._log_event("bounce", "Rebote", "Flanco descartado por antirrebote")

        elif isinstance(message, DeviceError):
            self._log_event("error", "Error", f"{message.code}: {message.msg}")

    # ------------------------------------------------------------------
    # Salud del enlace
    # ------------------------------------------------------------------

    def _on_health(self, health: Health) -> None:
        """Transicion de salud emitida por el lector (hilo de Qt por el puente)."""
        previous, self._last_health = self._last_health, health
        self._apply_health(health)
        if health is Health.OFFLINE:
            self._handle_link_lost()
        elif health in (Health.ONLINE, Health.DEGRADED) and previous in (
            Health.OFFLINE,
            None,
        ):
            self._handle_link_back()

    def _handle_link_lost(self) -> None:
        """Avisa una sola vez de que el Arduino ya no responde.

        Distingue una caida real de una placa que nunca llego a conectarse: si
        no hubo ningun ``hello``, no hay nada que se haya perdido y avisar al
        arrancar sin hardware seria ruido.
        """
        manager = self._manager
        if manager is None or self._link_lost or self._expect_disconnect:
            return
        if not manager.ever_online:
            self._status("Sin enlace todavía: esperando a que la placa responda.")
            return

        self._link_lost = True
        detail = "Arduino desconectado: la alarma no está disponible"
        self._log_event("error", "Sin sensor", detail)
        self._status(f"{detail}. Se reintentará automáticamente.")

        hour = time.strftime("%H:%M:%S")
        text = self._config.tts.text(
            "device_lost", hora=hour, evento="Arduino desconectado", zona="", duracion=""
        )
        spoken = self._speech.say_event("device_lost", text)
        if spoken and self._config.tts.event("device_lost").log:
            self._log_event("info", "Voz", text)

        self._notify(
            "device_lost",
            "Arduino desconectado",
            "La alarma no está disponible: mientras no vuelva el sensor no se "
            "detectará movimiento.",
        )

    def _handle_link_back(self) -> None:
        if not self._link_lost:
            return
        self._link_lost = False
        self._log_event("info", "Sensor recuperado", "Enlace restablecido con el Arduino")
        text = self._config.tts.text("device_ready")
        if (
            self._speech.say_event("device_ready", text)
            and self._config.tts.event("device_ready").log
        ):
            self._log_event("info", "Voz", text)
        self._status("Enlace restablecido: la alarma vuelve a estar disponible.")

    # ------------------------------------------------------------------
    # Registro
    # ------------------------------------------------------------------

    def _open_store(self, path: Path | str | None) -> EventStore:
        """Abre el historial; si el disco falla, se sigue en memoria."""
        cfg = self._config.log
        target: Path | str
        if path is not None:
            target = path
        else:
            target = Path(cfg.db_path)
            if not Path(target).is_absolute():
                # Relativa a la raiz de datos, no al directorio desde el que se
                # lanzó: un `.exe` abierto desde el Explorador puede tener
                # cualquier `cwd`, y el historial no debe cambiar de sitio.
                target = STORAGE_ROOT / target
        try:
            store = EventStore(target, max_events=cfg.max_events, max_age_days=cfg.max_age_days)
            store.purge()
            return store
        except Exception as exc:  # noqa: BLE001 - el historial no es critico
            log.exception("no se pudo abrir el historial en %s", target)
            self._store_error = str(exc)
            return EventStore(":memory:", max_events=cfg.max_events, max_age_days=cfg.max_age_days)

    def _load_history(self) -> None:
        """Reconstruye el arbol con las ultimas filas guardadas."""
        try:
            rows = self._store.recent(self._config.log.visible_rows)
        except Exception:  # noqa: BLE001
            log.exception("no se pudo leer el historial")
            rows = []
        for row in rows:
            self._log.add_row(row.tag, row.kind, row.detail, ts=row.ts, scroll=False)
        self._refresh_log_footer()

    def _log_event(self, tag: str, kind: str, detail: str) -> None:
        try:
            row = self._store.append(tag, kind, detail)
        except Exception as exc:  # noqa: BLE001 - el registro no tumba la app
            log.exception("no se pudo guardar el evento")
            self._log.add_row(tag, kind, detail)
            self._log.add_row("error", "Historial", f"no se pudo guardar en disco: {exc}")
            return
        self._log.add_row(tag, kind, detail, ts=row.ts)
        self._refresh_log_footer()

    def _refresh_log_footer(self) -> None:
        visible = self._log.topLevelItemCount()
        try:
            total = self._store.count()
        except Exception:  # noqa: BLE001
            total = visible
        retention = _retention_text(self._config.log.max_age_days)
        self._log_footer.setText(
            f"Mostrando las últimas {_thousands(visible)} de {_thousands(total)} · "
            f"retención {retention} / {_thousands(self._config.log.max_events)} eventos"
        )

    def _purge_history(self) -> None:
        try:
            removed = self._store.purge()
        except Exception:  # noqa: BLE001
            log.exception("fallo al aplicar la retención del historial")
            return
        if removed:
            self._refresh_log_footer()
            self._log_event("info", "Historial", f"retención aplicada: {removed} filas antiguas")

    def export_log(self) -> None:
        """Vuelca todo el historial a CSV, con nombre por defecto fechado."""
        default_name = f"registro-alarma-{time.strftime('%Y%m%d-%H%M')}.csv"
        suggested = str(Path.cwd() / "data" / default_name)
        chosen, _filter = QFileDialog.getSaveFileName(
            self,
            "Exportar registro",
            suggested,
            "CSV (*.csv);;Todos los ficheros (*)",
        )
        if not chosen:
            return
        target = Path(chosen)
        if target.suffix.lower() != ".csv":
            target = target.with_suffix(".csv")
        try:
            rows = self._store.export_csv(target)
        except Exception as exc:  # noqa: BLE001 - se informa en la ventana
            log.exception("no se pudo exportar el registro")
            self._log_event("error", "Exportar", f"no se pudo escribir {target}: {exc}")
            QMessageBox.critical(
                self, "No se pudo exportar", f"El registro no se ha guardado:\n{exc}"
            )
            return
        self._log_event("info", "Exportar", f"{rows} filas en {target}")
        self._status(f"Registro exportado: {rows} filas en {target}.")

    def export_config(self) -> None:
        """Vuelca la configuracion vigente a JSON, con las credenciales en blanco."""
        default_name = f"config-alarma-{time.strftime('%Y%m%d-%H%M')}.json"
        suggested = str(STORAGE_ROOT / "data" / default_name)
        chosen, _filter = QFileDialog.getSaveFileName(
            self,
            "Guardar configuración en JSON",
            suggested,
            "JSON (*.json);;Todos los ficheros (*)",
        )
        if not chosen:
            return
        target = Path(chosen)
        if target.suffix.lower() != ".json":
            target = target.with_suffix(".json")
        try:
            export_config_json(self._config, target)
        except Exception as exc:  # noqa: BLE001 - se informa en la ventana
            log.exception("no se pudo exportar la configuracion")
            self._log_event("error", "Exportar", f"no se pudo escribir {target}: {exc}")
            QMessageBox.critical(
                self, "No se pudo exportar", f"La configuración no se ha guardado:\n{exc}"
            )
            return
        self._log_event("info", "Exportar", f"configuración en {target}")
        self._status(f"Configuración exportada a {target} (sin credenciales).")

    def export_backup(self) -> None:
        """Copia completa en un solo fichero, credenciales incluidas.

        Se avisa antes de escribir: el fichero lleva la contrasena de correo, el
        token de SMS y el de ntfy en claro. Quien lo abra, los lee.
        """
        secrets = collect_secrets(self._config, self._env_path)
        detail = (
            f"Incluye {len(secrets)} credencial(es) de correo, SMS y ntfy en claro."
            if secrets
            else "Ahora mismo no hay credenciales guardadas, así que la copia no las llevará."
        )
        answer = QMessageBox.warning(
            self,
            "Copia completa",
            "Este fichero guarda toda la configuración —incluido el puerto serie— para "
            "poder montar la alarma en otro PC.\n\n"
            f"{detail}\n\n"
            "Cualquiera que lo abra podrá leerlas: guárdalo como guardarías una "
            "contraseña y bórralo cuando ya no lo necesites. Para compartir la "
            "configuración sin secretos, usa «Guardar configuración en JSON».",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return

        default_name = f"copia-alarma-{time.strftime('%Y%m%d-%H%M')}{EXTENSION}"
        suggested = str(STORAGE_ROOT / "data" / default_name)
        chosen, _filter = QFileDialog.getSaveFileName(
            self,
            "Guardar copia completa",
            suggested,
            f"Copia de la alarma (*{EXTENSION});;Todos los ficheros (*)",
        )
        if not chosen:
            return
        target = Path(chosen)
        if target.suffix.lower() != EXTENSION:
            target = target.with_suffix(EXTENSION)
        try:
            write_backup(build_backup(self._config, env_path=self._env_path), target)
        except Exception as exc:  # noqa: BLE001 - se informa en la ventana
            log.exception("no se pudo guardar la copia completa")
            self._log_event("error", "Copia", f"no se pudo escribir {target}: {exc}")
            QMessageBox.critical(self, "No se pudo guardar", f"La copia no se ha escrito:\n{exc}")
            return
        self._log_event("info", "Copia", f"copia completa en {target}")
        self._status(f"Copia completa guardada en {target}. Contiene credenciales.")

    def restore_backup(self) -> None:
        """Aplica una copia completa hecha en este u otro PC."""
        chosen, _filter = QFileDialog.getOpenFileName(
            self,
            "Restaurar copia completa",
            str(STORAGE_ROOT / "data"),
            f"Copia de la alarma (*{EXTENSION});;JSON (*.json);;Todos los ficheros (*)",
        )
        if not chosen:
            return
        try:
            backup = read_backup(chosen)
        except BackupError as exc:
            self._log_event("error", "Restaurar", str(exc))
            QMessageBox.critical(self, "Copia no válida", str(exc))
            return

        if not self._confirm_restore(backup):
            return

        previous = self._config
        try:
            restored = backup_restore(backup, local_path=self._local_path, env_path=self._env_path)
            config = load_config(
                self._config_path, env=load_env(self._env_path), local=self._local_path
            )
        except (BackupError, ConfigError) as exc:
            self._log_event("error", "Restaurar", str(exc))
            QMessageBox.critical(self, "No se pudo restaurar", str(exc))
            return
        except OSError as exc:
            log.exception("no se pudo restaurar la copia")
            self._log_event("error", "Restaurar", f"{exc}")
            QMessageBox.critical(
                self, "No se pudo restaurar", f"No se pudo escribir la configuración:\n{exc}"
            )
            return

        self._apply_config(config)
        self._log_event(
            "info",
            "Restaurar",
            f"{len(restored.sections)} secciones y {len(restored.secret_keys)} credencial(es) "
            f"desde {Path(chosen).name}",
        )
        self._status("Copia restaurada.")

        serial_changed = config.serial != previous.serial
        history_changed = config.log.db_path != previous.log.db_path
        message = (
            "Configuración restaurada.\n\n"
            f"Secciones: {', '.join(restored.sections)}.\n"
            f"Credenciales actualizadas: {len(restored.secret_keys)}.\n"
            "El historial de eventos no se ha tocado."
        )
        if serial_changed:
            message += (
                "\n\nLa copia trae otro puerto serie o baudios: reconecta con la placa "
                "para usarlos."
            )
        if history_changed:
            message += (
                f"\n\nLa copia apunta el historial a {config.log.db_path}: reinicia para abrirlo."
            )
        QMessageBox.information(self, "Copia restaurada", message)

    def _confirm_restore(self, backup: Backup) -> bool:
        """Resumen de lo que se va a pisar, antes de pisarlo."""
        created = backup.created.replace("T", " ") or "fecha desconocida"
        secrets_text = (
            f"{len(backup.secret_keys)} credencial(es): {', '.join(backup.secret_keys)}"
            if backup.secrets
            else "ninguna"
        )
        answer = QMessageBox.question(
            self,
            "Restaurar copia completa",
            f"Copia del {created}.\n"
            f"Versión {backup.version or '?'} · equipo {backup.machine or '?'}.\n"
            f"Secciones: {', '.join(backup.sections) or 'ninguna'}.\n"
            f"Credenciales: {secrets_text}.\n\n"
            f"Se sobrescribe {self._local_path.name} y se actualizan esas claves en el "
            ".env de este PC. El historial de eventos no se toca.\n\n"
            "¿Restaurar esta copia?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer is QMessageBox.StandardButton.Yes

    def _confirm_clear_log(self) -> None:
        """Borra el historial solo tras confirmarlo: no tiene vuelta atras."""
        try:
            total = self._store.count()
        except Exception:  # noqa: BLE001
            total = self._log.topLevelItemCount()
        if total <= 0:
            self._status("El registro ya está vacío.")
            return
        answer = QMessageBox.question(
            self,
            "Vaciar el registro",
            f"¿Vaciar el registro?\n\nSe borrarán {_thousands(total)} eventos de la base "
            "de datos y no se pueden recuperar.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self.clear_log()

    def clear_log(self) -> None:
        try:
            self._store.clear()
        except Exception:  # noqa: BLE001
            log.exception("no se pudo vaciar el historial")
        self._log.clear_rows()
        self._events = 0
        self._pulses = 0
        self._events_label.setText("0")
        self._pulses_label.setText("0")
        self._refresh_log_footer()
        self._status("Registro vaciado.")

    # ------------------------------------------------------------------
    # Refresco periodico
    # ------------------------------------------------------------------

    def _tick_delays(self) -> None:
        """Avanza la cuenta atras del retardo de salida o de entrada.

        Se llama desde un temporizador de la ventana y no desde el hilo del
        puerto: el estado lo mueve un solo hilo, igual que las ordenes del
        usuario, y asi no hay carreras entre armar y que venza el retardo.
        """
        transition = self._controller.tick()
        if transition is not None:
            self._apply_transition(transition)
            return
        if self._controller.remaining_s() is not None:
            self._paint_countdown()

    def _refresh(self) -> None:
        manager = self._manager
        if manager is None:
            if self._last_health is not Health.UNKNOWN:
                self._last_health = Health.UNKNOWN
                self._apply_health(Health.UNKNOWN)
            self._paint_connection(None)
            return

        if not manager.alive and not self._expect_disconnect:
            self._log_event("error", "Enlace", "el lector se ha detenido; reiniciando")
            with suppress(Exception):
                self._restart_manager()
            return

        health = manager.health
        if health is not self._last_health:
            self._on_health(health)
        else:
            # La cuenta atras del reintento cambia sin cambiar de estado.
            self._paint_connection(health)
        # La franja del sensor avanza aunque no llegue ningun evento: asi se ve
        # que lleva 60 s escuchando aunque no haya pasado nadie.
        self._sensor.tick()
        self._paint_warmup()
        if health is Health.OFFLINE and manager.stats.last_error:
            self._status(f"Sin enlace: {manager.stats.last_error}")

    # ------------------------------------------------------------------
    # Cierre
    # ------------------------------------------------------------------

    def _warn_not_connected(self) -> None:
        QMessageBox.warning(
            self,
            "Sin dispositivo",
            "No hay ningún dispositivo conectado.\n\n"
            "Elige un puerto y pulsa «Conectar», o marca «Simulado» para probar la "
            "aplicación sin hardware.",
        )

    def shutdown(self) -> None:
        """Detiene hilos y temporizadores sin destruir la ventana."""
        for timer in (
            getattr(self, "_timer", None),
            getattr(self, "_delay_timer", None),
            getattr(self, "_port_timer", None),
            getattr(self, "_purge_timer", None),
        ):
            if timer is not None:
                with suppress(Exception):
                    timer.stop()
        if self._calib_dialog is not None:
            with suppress(Exception):
                self._calib_dialog.close()
            self._calib_dialog = None
        listener, self._remote_listener = self._remote_listener, None
        if listener is not None:
            with suppress(Exception):
                listener.stop()
        self._notifier.close()
        self._speech.close()
        self.disconnect()
        with suppress(Exception):
            self._store.close()

    def closeEvent(self, event: QCloseEvent | None) -> None:  # noqa: N802 - API de Qt
        self.shutdown()
        super().closeEvent(event)


def _enable_dpi_awareness() -> None:
    """Evita que Windows escale la ventana y la deje borrosa en pantallas HiDPI."""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # pragma: no cover - no es Windows o ya esta activo
        pass


def _install_excepthook(window: MainWindow) -> None:
    """Impide que una excepcion suelta cierre la aplicacion.

    PyQt6 llama a `sys.excepthook` cuando una excepcion sale de un slot y el
    gestor por defecto aborta el proceso: la ventana desaparece sin dejar
    rastro. Con este gancho se registra la traza, se avisa en la barra de
    estado y la aplicacion sigue funcionando.
    """
    previous = sys.excepthook

    def hook(exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)  # type: ignore[arg-type]
            return
        window.report_ui_error(exc)

    sys.excepthook = hook


def run(
    *,
    simulated: bool = False,
    port: str | None = None,
    baud: int | None = None,
    sim_speed: float = 1.0,
    config: AppConfig | None = None,
) -> int:
    _enable_dpi_awareness()
    instance = QApplication.instance()
    app = instance if isinstance(instance, QApplication) else QApplication([])
    theme.apply(app)
    # `--port` manda; si no se pasa, vale lo que diga la configuracion. «auto»
    # se traduce a None para que el enlace busque la placa por VID/PID.
    chosen_port = port
    if chosen_port is None and config is not None:
        configured = config.serial.port.strip()
        chosen_port = None if configured.lower() in ("", "auto") else configured
    chosen_baud = baud if baud is not None else (config.serial.baudrate if config else 115200)
    window = MainWindow(
        simulated=simulated,
        port=chosen_port,
        baud=chosen_baud,
        sim_speed=sim_speed,
        config=config,
    )
    window.show()
    _install_excepthook(window)
    return int(app.exec())
