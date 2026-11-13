"""Configuracion de la aplicacion.

TOML en lugar de YAML por una razon concreta: `tomllib` viene con Python desde
3.11, mientras que YAML obliga a anadir PyYAML. El proyecto ya es deliberadamente
de dependencias minimas (ver ADR-014).

Hay dos capas de fichero:

- ``config/alarm.toml``: la configuracion base, con comentarios, editable a mano.
- ``config/alarm.local.toml``: lo que escriben los dialogos de la aplicacion.

Separarlas evita que guardar desde la interfaz borre los comentarios del fichero
base. La capa local se mezcla encima de la base tabla por tabla.

Los secretos no viven aqui: el TOML se versiona como ejemplo y las credenciales
(SMTP, Twilio) se leen de `.env`, que nunca entra en el repositorio.

Precedencia: valores por defecto < ``alarm.toml`` < ``alarm.local.toml`` < entorno.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .paths import resource_path, storage_root

log = logging.getLogger(__name__)

# Las rutas se resuelven una vez al importar: junto al ejecutable cuando la
# aplicacion va empaquetada, en el directorio actual en desarrollo. Ver
# `paths.py`. La configuracion de usuario se escribe en la raiz de datos, y el
# ejemplo se lee del paquete (dentro del .exe vive en una carpeta temporal).
STORAGE_ROOT = storage_root()
CONFIG_DIR = STORAGE_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "alarm.toml"
LOCAL_FILE = CONFIG_DIR / "alarm.local.toml"
EXAMPLE_FILE = CONFIG_DIR / "alarm.example.toml"
ENV_FILE = STORAGE_ROOT / ".env"

#: Nombre del TOML de ejemplo dentro del paquete y del repositorio.
EXAMPLE_NAME = "alarm.example.toml"

SECURITY_MODES = frozenset({"starttls", "ssl", "none"})
SMS_PROVIDERS = frozenset({"twilio", "email_gateway"})

#: Prioridades que entiende ntfy, de menos a mas urgente.
NTFY_PRIORITIES: tuple[str, ...] = ("min", "low", "default", "high", "urgent")
#: Valor numerico que espera la API para cada prioridad con nombre.
NTFY_PRIORITY_LEVELS: dict[str, int] = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}

#: Un tema de ntfy es un nombre corto: 1-64 caracteres de [A-Za-z0-9_-].
NTFY_TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")


def valid_ntfy_topic(topic: str) -> bool:
    """Si el nombre sirve como tema de ntfy."""
    return bool(NTFY_TOPIC_RE.match(topic))


def normalize_ntfy_server(server: str) -> str:
    """Base del servidor sin barra final; cadena vacia si no es http(s).

    Se acepta `ntfy.sh` sin esquema (se asume https) porque es lo que la gente
    escribe; lo que no se acepta es un esquema que no sea HTTP ni una URL con
    parametros, que solo puede acabar en una peticion rara.
    """
    text = server.strip()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    if parts.query or parts.fragment or parts.username or parts.password:
        return ""
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


# Causas de aviso que la aplicacion conoce. El orden importa: es el que se
# muestra en los menus y en los dialogos.
VOICE_KEYS: tuple[str, ...] = (
    "armed",
    "disarmed",
    "triggered",
    "rearmed",
    "device_lost",
    "device_ready",
)

# Textos por defecto. Van en el codigo ademas de en el ejemplo para que una
# configuracion a medias siga siendo util.
DEFAULT_MESSAGES: dict[str, str] = {
    "armed": "Alarma armada.",
    "disarmed": "Alarma desarmada.",
    "triggered": "Atencion. Se ha detectado una presencia a las {hora}.",
    "rearmed": (
        "Atención. La alarma se ha rearmado tras un disparo. Rearme {n} de {max}. Sigue vigilando."
    ),
    "device_lost": ("Atención. El Arduino se ha desconectado: la alarma no está disponible."),
    "device_ready": "Sensor operativo.",
}

# Etiquetas de estado que se pintan en la ventana principal.
DEFAULT_LABELS: dict[str, str] = {
    "disarmed": "DESARMADA",
    "arming": "ARMANDO",
    "armed": "ARMADA",
    "pending": "ENTRADA",
    "triggered": "DISPARADA",
    "rearming": "REARMANDO",
    "silenced": "SILENCIADA",
    "device_lost": "SIN SENSOR",
}


class ConfigError(ValueError):
    """Configuracion invalida. Se muestra al usuario y no se arranca a medias."""


@dataclass(frozen=True, slots=True)
class SerialConfig:
    """Como se abre el puerto y con cuanta insistencia se recupera.

    ``auto_connect`` se aplica al arrancar: la placa debe quedar conectada sin
    que el usuario pulse nada. ``heartbeat_timeout_s`` es cuantos segundos sin
    recibir una linea se toleran antes de dar el enlace por perdido; el
    firmware late cada 2 s, asi que 8 s son cuatro latidos perdidos.
    """

    port: str = "auto"
    baudrate: int = 115200
    auto_connect: bool = True
    reconnect_max_s: float = 30.0
    heartbeat_timeout_s: float = 8.0


@dataclass(frozen=True, slots=True)
class AlarmRulesConfig:
    armed_on_start: bool = False
    cooldown_s: int = 180
    max_per_hour: int = 6
    # Retardos de una alarma profesional. 0 los desactiva: armar vigila al
    # instante y detectar dispara al instante. Los valores por defecto son los
    # habituales de las centrales de mercado: 45 s para salir y 30 s para
    # llegar al teclado y desarmar.
    exit_delay_s: int = 45
    entry_delay_s: int = 30
    # Rearme automatico tras un disparo: al terminar la sirena se espera
    # `rearm_delay_s` y la alarma vuelve a vigilar sola, hasta `max_rearms`
    # veces. El tope existe porque rearmarse sin limite ante un sensor
    # defectuoso convertiria la sirena en un bucle infinito.
    auto_rearm: bool = False
    rearm_delay_s: int = 300
    max_rearms: int = 3


@dataclass(frozen=True, slots=True)
class VoiceEventConfig:
    """Un aviso hablado configurable.

    ``label`` es el texto que se pinta como estado en la ventana principal, que
    no tiene por que coincidir con lo que se dice en voz alta: en pantalla se
    busca algo corto y en mayusculas, en el altavoz una frase completa.

    Los tres tiempos son distintos a proposito: ``delay_ms`` es la espera antes
    de empezar a hablar (para no pisar la sirena), ``gap_ms`` la pausa entre una
    repeticion y la siguiente, y ``repeat`` cuantas veces se dice. ``log`` decide
    si la locucion deja una fila en el registro: el evento de estado se apunta
    siempre, pero repetir en el historial lo que se ha dicho es opcional.
    """

    enabled: bool = True
    text: str = ""
    label: str = ""
    repeat: int = 2
    gap_ms: int = 0
    delay_ms: int = 0
    log: bool = True


def _voice_event(key: str, raw: Mapping[str, Any]) -> VoiceEventConfig:
    where = f"tts.events.{key}"
    default = VoiceEventConfig(
        text=DEFAULT_MESSAGES.get(key, key),
        label=DEFAULT_LABELS.get(key, key.upper()),
        repeat=2,
        gap_ms=0,
        delay_ms=0,
        log=True,
    )
    return VoiceEventConfig(
        enabled=_bool(raw, "enabled", default.enabled, where),
        text=_str(raw, "text", default.text, where),
        label=_str(raw, "label", default.label, where),
        repeat=_int(raw, "repeat", default.repeat, where, 0, 10),
        gap_ms=_int(raw, "gap_ms", default.gap_ms, where, 0, 30000),
        delay_ms=_int(raw, "delay_ms", default.delay_ms, where, 0, 600000),
        log=_bool(raw, "log", default.log, where),
    )


@dataclass(frozen=True, slots=True)
class TtsConfig:
    enabled: bool = True
    voice: str = "Microsoft Helena Desktop"
    rate: int = 0
    volume: int = 90
    repeat: int = 2
    events: Mapping[str, VoiceEventConfig] = field(default_factory=dict)

    def event(self, key: str) -> VoiceEventConfig:
        """Configuracion del aviso, con los valores por defecto si falta."""
        found = self.events.get(key)
        if found is not None:
            return found
        return VoiceEventConfig(
            text=DEFAULT_MESSAGES.get(key, key),
            label=DEFAULT_LABELS.get(key, key.upper()),
            repeat=self.repeat,
            gap_ms=0,
            delay_ms=0,
            log=True,
        )

    def text(self, key: str, **values: str) -> str:
        """Plantilla del mensaje con los valores sustituidos.

        Una clave ausente o una plantilla con un campo desconocido no deben
        romper el aviso: se degrada al texto por defecto y se registra.
        """
        event = self.event(key)
        template = event.text or DEFAULT_MESSAGES.get(key, key)
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            log.warning("plantilla de voz invalida para %r: %r", key, template)
            return template

    @property
    def messages(self) -> dict[str, str]:
        """Compatibilidad: vista ``clave -> texto`` de los eventos."""
        return {key: self.event(key).text for key in VOICE_KEYS}


@dataclass(frozen=True, slots=True)
class EmailConfig:
    enabled: bool = False
    on_armed: bool = True
    on_disarmed: bool = False
    on_triggered: bool = True
    host: str = "smtp.gmail.com"
    port: int = 587
    security: str = "starttls"
    sender: str = ""
    to: tuple[str, ...] = ()
    subject: str = "[Alarma] {evento} a las {hora}"
    timeout_s: float = 15.0
    retries: int = 2
    user: str = ""
    password: str = ""
    on_device_lost: bool = True

    @property
    def configured(self) -> bool:
        """Hay datos suficientes para intentar el envio."""
        return bool(self.host and self.sender and self.to and self.user and self.password)

    def enabled_for(self, cause: str) -> bool:
        return {
            "armed": self.on_armed,
            "disarmed": self.on_disarmed,
            "triggered": self.on_triggered,
            "device_lost": self.on_device_lost,
        }.get(cause, False)


@dataclass(frozen=True, slots=True)
class SmsConfig:
    """Avisos por SMS con dos proveedores intercambiables.

    ``twilio`` usa la API HTTP; ``email_gateway`` reutiliza el SMTP y manda a
    la direccion ``<numero>@<dominio>`` del operador. La pasarela es gratuita
    pero cada vez menos fiable: por eso el proveedor es configurable y no una
    suposicion del codigo.
    """

    enabled: bool = False
    provider: str = "twilio"
    on_armed: bool = False
    on_disarmed: bool = False
    on_triggered: bool = True
    to: tuple[str, ...] = ()
    from_number: str = ""
    gateway_domain: str = ""
    template: str = "[Alarma] {evento} a las {hora}"
    timeout_s: float = 10.0
    retries: int = 1
    sid: str = ""
    token: str = ""
    on_device_lost: bool = True

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """Datos que faltan para intentar el envio, con nombres de la interfaz.

        Un «faltan SID, token, remitente o destinatarios» obliga a adivinar
        cual de los cuatro es. Devolver la lista concreta permite que el
        dialogo diga justo lo que hay que rellenar.
        """
        if not self.to:
            return ("destinatarios",)
        if self.provider == "twilio":
            faltan: list[str] = []
            if not self.sid:
                faltan.append("cuenta SID")
            if not self.token:
                faltan.append("token de autenticación")
            if not self.from_number:
                faltan.append("número remitente")
            return tuple(faltan)
        return () if self.gateway_domain else ("dominio de la pasarela",)

    @property
    def configured(self) -> bool:
        return not self.missing_fields

    def enabled_for(self, cause: str) -> bool:
        return {
            "armed": self.on_armed,
            "disarmed": self.on_disarmed,
            "triggered": self.on_triggered,
            "device_lost": self.on_device_lost,
        }.get(cause, False)


@dataclass(frozen=True, slots=True)
class NtfyConfig:
    """Avisos push al movil por ntfy.

    ntfy es un servicio de notificaciones por HTTPS: el programa publica en un
    tema y la aplicacion del movil, suscrita a ese tema, muestra el aviso. No
    hay SMS, ni tarifa, ni tarjeta SIM, y sirve igual en Android y en iPhone.

    En el servidor publico un tema es publico: quien conozca su nombre puede
    suscribirse. Por eso el nombre debe ser largo y aleatorio, y quien quiera
    control de acceso de verdad puede apuntar `server` a su propia instancia y
    guardar el token en `.env` (`ALARM_NTFY_TOKEN`).
    """

    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic: str = ""
    on_armed: bool = False
    on_disarmed: bool = False
    on_triggered: bool = True
    on_device_lost: bool = True
    title: str = "[Alarma] {evento}"
    priority_triggered: str = "urgent"
    priority_device_lost: str = "high"
    priority_armed: str = "default"
    priority_disarmed: str = "default"
    timeout_s: float = 10.0
    retries: int = 1
    token: str = ""

    @property
    def base_url(self) -> str:
        """Raiz del servidor donde se publica, sin barra final."""
        return normalize_ntfy_server(self.server)

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """Datos que faltan para intentar el envio, con nombres de la interfaz."""
        faltan: list[str] = []
        if not self.base_url:
            faltan.append("servidor")
        if not valid_ntfy_topic(self.topic):
            faltan.append("tema")
        return tuple(faltan)

    @property
    def configured(self) -> bool:
        return not self.missing_fields

    @property
    def uses_public_server(self) -> bool:
        """Si publica en `ntfy.sh`, donde el tema no es privado."""
        parts = urlsplit(self.base_url) if self.base_url else None
        return parts is not None and (parts.hostname or "").lower().endswith("ntfy.sh")

    def enabled_for(self, cause: str) -> bool:
        return {
            "armed": self.on_armed,
            "disarmed": self.on_disarmed,
            "triggered": self.on_triggered,
            "device_lost": self.on_device_lost,
        }.get(cause, False)

    def priority_for(self, cause: str) -> str:
        """Prioridad con nombre segun el evento, con «default» como red."""
        value = {
            "triggered": self.priority_triggered,
            "device_lost": self.priority_device_lost,
            "armed": self.priority_armed,
            "disarmed": self.priority_disarmed,
        }.get(cause, "default")
        return value if value in NTFY_PRIORITY_LEVELS else "default"


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    """Mando remoto: el PC escucha ordenes publicadas en un tema de ntfy.

    Las ordenes viajan en un tema **distinto** del de los avisos, no por
    comodidad sino para poder darles permisos distintos en un servidor propio:
    el movil solo escribe ordenes y el PC solo las lee, asi una credencial
    filtrada del movil no permite espiar los avisos de la casa.

    La clave (`key`) y el secreto TOTP se leen de `.env`, nunca del TOML. Con
    `totp_secret` configurado la clave deja de aceptarse: un codigo de seis
    digitos no viaja en el mensaje, y en el servicio publico el mensaje lo lee
    cualquiera que conozca el tema.
    """

    enabled: bool = False
    #: Vacio: se usa `<tema de avisos>-mando`.
    topic: str = ""
    allow_arm: bool = True
    allow_disarm: bool = True
    allow_status: bool = True
    allow_silence: bool = True
    #: Hace sonar la sirena sin esperar al sensor. Apagado a proposito.
    allow_panic: bool = False
    reply: bool = True
    #: Fallos seguidos antes de ignorar ordenes; 0 desactiva el bloqueo.
    lockout_failures: int = 5
    lockout_minutes: int = 15
    key: str = ""
    totp_secret: str = ""

    @property
    def effective_topic(self) -> str:
        """Tema fijado a mano, sin derivar; vacio si no se ha fijado."""
        return self.topic.strip()

    def topic_for(self, ntfy: NtfyConfig) -> str:
        """Tema de mando efectivo, con el sufijo cuando no hay uno explicito."""
        if self.topic.strip():
            return self.topic.strip()
        return f"{ntfy.topic}-mando" if ntfy.topic else ""

    @property
    def uses_totp(self) -> bool:
        return bool(self.totp_secret)

    @property
    def authenticates(self) -> bool:
        """Si hay con que comprobar una orden."""
        return bool(self.key or self.totp_secret)

    @property
    def allows(self) -> tuple[str, ...]:
        """Ordenes que acepta esta configuracion, con sus nombres internos."""
        pairs = (
            ("arm", self.allow_arm),
            ("disarm", self.allow_disarm),
            ("status", self.allow_status),
            ("silence", self.allow_silence),
            ("panic", self.allow_panic),
        )
        return tuple(name for name, allowed in pairs if allowed)

    def allows_action(self, action: str) -> bool:
        return action in self.allows


@dataclass(frozen=True, slots=True)
class UiConfig:
    labels: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_LABELS))
    show_heartbeats: bool = False
    beeps: bool = True

    def label(self, key: str) -> str:
        return self.labels.get(key) or DEFAULT_LABELS.get(key, key.upper())


@dataclass(frozen=True, slots=True)
class BuzzerConfig:
    enabled: bool = True
    pin: int = 11
    # Dos minutos por defecto: una sirena de 15 s se apaga antes de que nadie
    # reaccione, y el tope de 10 min evita que un fallo deje el piezo sonando
    # toda la tarde si nadie desarma.
    siren_ms: int = 120_000
    # Valores de la prueba manual: frecuencia y duracion del tono continuo.
    # El piezo barato resuena entre 2 y 4 kHz; guardarlos evita repetir el
    # ajuste cada vez que se comprueba el cableado.
    test_hz: int = 2000
    test_ms: int = 1200


@dataclass(frozen=True, slots=True)
class LogConfig:
    """Retencion del historial de eventos.

    Dos limites que se aplican a la vez, no alternativos: se borra lo que
    incumpla cualquiera de los dos. Un año son ~3.000 filas con uso normal; el
    tope de 100.000 existe para que una averia que genere eventos en bucle no
    llene el disco.
    """

    max_events: int = 100_000
    max_age_days: int = 365
    visible_rows: int = 1_000
    db_path: str = "data/alarm.db"


@dataclass(frozen=True, slots=True)
class AppConfig:
    serial: SerialConfig = field(default_factory=SerialConfig)
    alarm: AlarmRulesConfig = field(default_factory=AlarmRulesConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    sms: SmsConfig = field(default_factory=SmsConfig)
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    buzzer: BuzzerConfig = field(default_factory=BuzzerConfig)
    log: LogConfig = field(default_factory=LogConfig)
    source: str = "valores por defecto"

    @property
    def remote_topic(self) -> str:
        """Tema donde el PC escucha las ordenes del movil."""
        return self.remote.topic_for(self.ntfy)

    def remote_missing(self) -> tuple[str, ...]:
        """Lo que falta para poder escuchar ordenes, en palabras de la interfaz.

        No basta con que el mando este marcado: las ordenes llegan por el mismo
        servidor que los avisos, asi que el canal movil tiene que estar activo y
        configurado. Decirlo aqui evita que el usuario encienda el mando y no
        entienda por que no pasa nada.
        """
        faltan: list[str] = []
        if not self.ntfy.configured:
            faltan.append("datos del móvil (servidor y tema)")
        topic = self.remote_topic
        if not topic:
            faltan.append("tema de mando")
        elif not valid_ntfy_topic(topic):
            faltan.append("tema de mando válido")
        if not self.remote.authenticates:
            faltan.append("clave de mando o código TOTP")
        return tuple(faltan)


def _default_events(repeat: int = 2) -> dict[str, VoiceEventConfig]:
    return {
        key: VoiceEventConfig(
            text=DEFAULT_MESSAGES.get(key, key),
            label=DEFAULT_LABELS.get(key, key.upper()),
            repeat=repeat,
            gap_ms=0,
            delay_ms=0,
            log=True,
        )
        for key in VOICE_KEYS
    }


def default_config() -> AppConfig:
    return AppConfig(tts=TtsConfig(events=_default_events()), ui=UiConfig())


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------


def parse_env(text: str) -> dict[str, str]:
    """Parser minimo de `.env`: KEY=VALUE, comentarios con `#`, comillas opcionales."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(path: Path | None = None) -> dict[str, str]:
    """Lee `.env` y lo superpone al entorno del proceso.

    El fichero tiene prioridad sobre el entorno del sistema: es lo que espera
    quien edita `.env` para cambiar la configuracion sin tocar Windows.
    """
    target = ENV_FILE if path is None else Path(path)
    merged = dict(os.environ)
    if not target.is_file():
        return merged
    try:
        merged.update(parse_env(target.read_text(encoding="utf-8")))
    except OSError as exc:
        log.warning("no se pudo leer %s: %s", target, exc)
    return merged


# ---------------------------------------------------------------------------
# Validacion de tablas
# ---------------------------------------------------------------------------


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' debe ser una tabla")
    return value


def _bool(table: Mapping[str, Any], key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}.{key} debe ser true o false")
    return value


def _int(table: Mapping[str, Any], key: str, default: int, where: str, lo: int, hi: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key} debe ser un numero entero")
    if not lo <= value <= hi:
        raise ConfigError(f"{where}.{key} debe estar entre {lo} y {hi}")
    return int(value)


def _float(
    table: Mapping[str, Any], key: str, default: float, where: str, lo: float, hi: float
) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key} debe ser un numero")
    if not lo <= float(value) <= hi:
        raise ConfigError(f"{where}.{key} debe estar entre {lo} y {hi}")
    return float(value)


def _str(table: Mapping[str, Any], key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} debe ser texto")
    return value


def _str_tuple(
    table: Mapping[str, Any], key: str, default: tuple[str, ...], where: str
) -> tuple[str, ...]:
    value = table.get(key, list(default))
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f"{where}.{key} debe ser una lista de textos")
    return tuple(str(v).strip() for v in value if str(v).strip())


def _priority(table: Mapping[str, Any], key: str, default: str, where: str) -> str:
    """Una prioridad de ntfy, validada contra la lista conocida."""
    value = _str(table, key, default, where).strip().lower()
    if value not in NTFY_PRIORITIES:
        raise ConfigError(f"{where}.{key} debe ser {', '.join(NTFY_PRIORITIES)}")
    return value


def _section_texts(table: Mapping[str, Any], key: str, where: str) -> dict[str, str]:
    raw = _table(table, key)
    texts: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(value, str):
            raise ConfigError(f"{where}.{key}.{name} debe ser texto")
        texts[str(name)] = value
    return texts


# ---------------------------------------------------------------------------
# Construccion
# ---------------------------------------------------------------------------


def _voice_events(tts_raw: Mapping[str, Any], repeat: int) -> dict[str, VoiceEventConfig]:
    """Lee ``[tts.events.<clave>]`` y migra ``[tts.messages]`` si hace falta.

    La migracion es solo en memoria: quien guarde desde la interfaz escribira
    ya el formato nuevo en la capa local.
    """
    events = _default_events(repeat)
    legacy = _section_texts(tts_raw, "messages", "tts") if "messages" in tts_raw else {}

    raw_events = _table(tts_raw, "events") if "events" in tts_raw else {}
    for key in VOICE_KEYS:
        merged: dict[str, Any] = {}
        if key in legacy:
            merged["text"] = legacy[key]
        entry = raw_events.get(key)
        if entry is not None:
            if not isinstance(entry, dict):
                raise ConfigError(f"tts.events.{key} debe ser una tabla")
            merged.update(entry)
        if merged:
            events[key] = _voice_event(key, merged)
    return events


def _remote_config(remote_raw: Mapping[str, Any], ntfy_topic: str) -> RemoteConfig:
    """Lee ``[remote]`` y valida el tema de mando efectivo.

    El tema de mando se valida ya resuelto: si viene vacio se deriva del de
    avisos con el sufijo `-mando`, y el resultado tiene que seguir cabiendo en
    los 64 caracteres de ntfy. Es preferible un error al cargar que una orden
    que el servidor rechaza con un 400 en mitad de una urgencia.
    """
    topic = _str(remote_raw, "topic", "", "remote").strip()
    if topic and not valid_ntfy_topic(topic):
        raise ConfigError("remote.topic solo admite letras, números, guion y guion bajo (1 a 64)")
    effective = topic or (f"{ntfy_topic}-mando" if ntfy_topic else "")
    if len(effective) > 64:
        raise ConfigError(
            "el tema de mando pasa de 64 caracteres: pon uno más corto en remote.topic"
        )

    def allowed_flag(name: str, default: bool) -> bool:
        return _bool(remote_raw, name, default, "remote")

    return RemoteConfig(
        enabled=_bool(remote_raw, "enabled", False, "remote"),
        topic=topic,
        allow_arm=allowed_flag("allow_arm", True),
        allow_disarm=allowed_flag("allow_disarm", True),
        allow_status=allowed_flag("allow_status", True),
        allow_silence=allowed_flag("allow_silence", True),
        allow_panic=allowed_flag("allow_panic", False),
        reply=_bool(remote_raw, "reply", True, "remote"),
        lockout_failures=_int(remote_raw, "lockout_failures", 5, "remote", 0, 50),
        lockout_minutes=_int(remote_raw, "lockout_minutes", 15, "remote", 1, 1440),
    )


def from_mapping(data: Mapping[str, Any]) -> AppConfig:
    """Valida el contenido TOML y construye la configuracion tipada."""
    serial_raw = _table(data, "serial")
    alarm_raw = _table(data, "alarm")
    tts_raw = _table(data, "tts")
    notify_raw = _table(data, "notify")
    email_raw = _table(notify_raw, "email")
    sms_raw = _table(notify_raw, "sms")
    ntfy_raw = _table(notify_raw, "ntfy")
    buzzer_raw = _table(data, "buzzer")

    serial = SerialConfig(
        port=_str(serial_raw, "port", "auto", "serial"),
        baudrate=_int(serial_raw, "baudrate", 115200, "serial", 1200, 921600),
        auto_connect=_bool(serial_raw, "auto_connect", True, "serial"),
        reconnect_max_s=_float(serial_raw, "reconnect_max_s", 30.0, "serial", 1.0, 600.0),
        heartbeat_timeout_s=_float(serial_raw, "heartbeat_timeout_s", 8.0, "serial", 2.0, 300.0),
    )

    rules = AlarmRulesConfig(
        armed_on_start=_bool(alarm_raw, "armed_on_start", False, "alarm"),
        cooldown_s=_int(alarm_raw, "cooldown_s", 180, "alarm", 0, 86400),
        max_per_hour=_int(alarm_raw, "max_per_hour", 6, "alarm", 1, 1000),
        exit_delay_s=_int(alarm_raw, "exit_delay_s", 45, "alarm", 0, 300),
        entry_delay_s=_int(alarm_raw, "entry_delay_s", 30, "alarm", 0, 300),
        auto_rearm=_bool(alarm_raw, "auto_rearm", False, "alarm"),
        rearm_delay_s=_int(alarm_raw, "rearm_delay_s", 300, "alarm", 0, 86400),
        max_rearms=_int(alarm_raw, "max_rearms", 3, "alarm", 1, 10),
    )

    repeat = _int(tts_raw, "repeat", 2, "tts", 1, 10)
    tts = TtsConfig(
        enabled=_bool(tts_raw, "enabled", True, "tts"),
        voice=_str(tts_raw, "voice", "Microsoft Helena Desktop", "tts"),
        rate=_int(tts_raw, "rate", 0, "tts", -10, 10),
        volume=_int(tts_raw, "volume", 90, "tts", 0, 100),
        repeat=repeat,
        events=_voice_events(tts_raw, repeat),
    )

    security = _str(email_raw, "security", "starttls", "notify.email").lower()
    if security not in SECURITY_MODES:
        raise ConfigError("notify.email.security debe ser starttls, ssl o none")

    email = EmailConfig(
        enabled=_bool(email_raw, "enabled", False, "notify.email"),
        on_armed=_bool(email_raw, "on_armed", True, "notify.email"),
        on_disarmed=_bool(email_raw, "on_disarmed", False, "notify.email"),
        on_triggered=_bool(email_raw, "on_triggered", True, "notify.email"),
        host=_str(email_raw, "host", "", "notify.email"),
        port=_int(email_raw, "port", 587, "notify.email", 1, 65535),
        security=security,
        sender=_str(email_raw, "from", "", "notify.email"),
        to=_str_tuple(email_raw, "to", (), "notify.email"),
        subject=_str(email_raw, "subject", "[Alarma] {evento} a las {hora}", "notify.email"),
        timeout_s=_float(email_raw, "timeout_s", 15.0, "notify.email", 1.0, 300.0),
        retries=_int(email_raw, "retries", 2, "notify.email", 0, 10),
        on_device_lost=_bool(email_raw, "on_device_lost", True, "notify.email"),
    )

    provider = _str(sms_raw, "provider", "twilio", "notify.sms").lower()
    if provider not in SMS_PROVIDERS:
        raise ConfigError("notify.sms.provider debe ser twilio o email_gateway")

    sms = SmsConfig(
        enabled=_bool(sms_raw, "enabled", False, "notify.sms"),
        provider=provider,
        on_armed=_bool(sms_raw, "on_armed", False, "notify.sms"),
        on_disarmed=_bool(sms_raw, "on_disarmed", False, "notify.sms"),
        on_triggered=_bool(sms_raw, "on_triggered", True, "notify.sms"),
        to=_str_tuple(sms_raw, "to", (), "notify.sms"),
        from_number=_str(sms_raw, "from_number", "", "notify.sms"),
        gateway_domain=_str(sms_raw, "gateway_domain", "", "notify.sms"),
        template=_str(sms_raw, "template", "[Alarma] {evento} a las {hora}", "notify.sms"),
        timeout_s=_float(sms_raw, "timeout_s", 10.0, "notify.sms", 1.0, 120.0),
        retries=_int(sms_raw, "retries", 1, "notify.sms", 0, 10),
        on_device_lost=_bool(sms_raw, "on_device_lost", True, "notify.sms"),
    )

    server = _str(ntfy_raw, "server", "https://ntfy.sh", "notify.ntfy").strip()
    if server and not normalize_ntfy_server(server):
        raise ConfigError(
            "notify.ntfy.server debe ser una dirección http(s), por ejemplo https://ntfy.sh"
        )
    topic = _str(ntfy_raw, "topic", "", "notify.ntfy").strip()
    if topic and not valid_ntfy_topic(topic):
        raise ConfigError(
            "notify.ntfy.topic solo admite letras, números, guion y guion bajo (1 a 64)"
        )
    ntfy = NtfyConfig(
        enabled=_bool(ntfy_raw, "enabled", False, "notify.ntfy"),
        server=server or "https://ntfy.sh",
        topic=topic,
        on_armed=_bool(ntfy_raw, "on_armed", False, "notify.ntfy"),
        on_disarmed=_bool(ntfy_raw, "on_disarmed", False, "notify.ntfy"),
        on_triggered=_bool(ntfy_raw, "on_triggered", True, "notify.ntfy"),
        on_device_lost=_bool(ntfy_raw, "on_device_lost", True, "notify.ntfy"),
        title=_str(ntfy_raw, "title", "[Alarma] {evento}", "notify.ntfy"),
        priority_triggered=_priority(ntfy_raw, "priority_triggered", "urgent", "notify.ntfy"),
        priority_device_lost=_priority(ntfy_raw, "priority_device_lost", "high", "notify.ntfy"),
        priority_armed=_priority(ntfy_raw, "priority_armed", "default", "notify.ntfy"),
        priority_disarmed=_priority(ntfy_raw, "priority_disarmed", "default", "notify.ntfy"),
        timeout_s=_float(ntfy_raw, "timeout_s", 10.0, "notify.ntfy", 1.0, 120.0),
        retries=_int(ntfy_raw, "retries", 1, "notify.ntfy", 0, 10),
    )

    remote = _remote_config(_table(data, "remote"), topic)

    ui_raw = _table(data, "ui")
    labels = dict(DEFAULT_LABELS)
    labels.update(_section_texts(ui_raw, "labels", "ui") if "labels" in ui_raw else {})
    buzzer = BuzzerConfig(
        enabled=_bool(buzzer_raw, "enabled", True, "buzzer"),
        pin=_int(buzzer_raw, "pin", 11, "buzzer", 2, 53),
        siren_ms=_int(buzzer_raw, "siren_ms", 120_000, "buzzer", 1000, 600_000),
        test_hz=_int(buzzer_raw, "test_hz", 2000, "buzzer", 100, 8000),
        test_ms=_int(buzzer_raw, "test_ms", 1200, "buzzer", 100, 10000),
    )

    log_raw = _table(data, "log")
    log_config = LogConfig(
        max_events=_int(log_raw, "max_events", 100_000, "log", 100, 5_000_000),
        max_age_days=_int(log_raw, "max_age_days", 365, "log", 1, 3650),
        visible_rows=_int(log_raw, "visible_rows", 1_000, "log", 100, 5_000),
        db_path=_str(log_raw, "db_path", "data/alarm.db", "log") or "data/alarm.db",
    )

    return AppConfig(
        serial=serial,
        alarm=rules,
        tts=tts,
        email=email,
        sms=sms,
        ntfy=ntfy,
        remote=remote,
        ui=UiConfig(
            labels=labels,
            show_heartbeats=_bool(ui_raw, "show_heartbeats", False, "ui"),
            beeps=_bool(ui_raw, "beeps", True, "ui"),
        ),
        buzzer=buzzer,
        log=log_config,
    )


def apply_env(config: AppConfig, env: Mapping[str, str]) -> AppConfig:
    """Superpone las credenciales de `.env` a la configuracion."""
    email_user = env.get("ALARM_SMTP_USER", "").strip()
    email_password = env.get("ALARM_SMTP_PASSWORD", "").strip()
    sms_sid = env.get("ALARM_TWILIO_SID", "").strip()
    sms_token = env.get("ALARM_TWILIO_TOKEN", "").strip()
    ntfy_token = env.get("ALARM_NTFY_TOKEN", "").strip()

    email = config.email
    if email_user or email_password:
        email = replace(email, user=email_user, password=email_password)

    sms = config.sms
    if sms_sid or sms_token:
        sms = replace(sms, sid=sms_sid, token=sms_token)

    ntfy = config.ntfy
    if ntfy_token:
        ntfy = replace(ntfy, token=ntfy_token)

    remote_key = env.get("ALARM_REMOTE_KEY", "").strip()
    remote_totp = env.get("ALARM_REMOTE_TOTP", "").strip()
    remote = config.remote
    if remote_key or remote_totp:
        remote = replace(remote, key=remote_key, totp_secret=remote_totp)

    if (
        email is config.email
        and sms is config.sms
        and ntfy is config.ntfy
        and remote is config.remote
    ):
        return config
    return replace(config, email=email, sms=sms, ntfy=ntfy, remote=remote)


# ---------------------------------------------------------------------------
# Carga
# ---------------------------------------------------------------------------


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Mezcla tablas TOML anidadas. `override` gana en las hojas."""
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def read_toml(path: Path) -> dict[str, Any]:
    """Lee un TOML. Un fichero ausente devuelve un diccionario vacio."""
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML invalido: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: no se pudo leer: {exc}") from exc
    return data


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    local: Path | None = None,
) -> AppConfig:
    """Carga la configuracion. Un fichero ausente no es un error."""
    target = CONFIG_FILE if path is None else Path(path)
    overlay = LOCAL_FILE if local is None else Path(local)
    environment = load_env() if env is None else env

    base_data = read_toml(target)
    local_data = read_toml(overlay)
    data = deep_merge(base_data, local_data)

    if not data:
        log.info("sin fichero de configuracion (%s); se usan los valores por defecto", target)
        return apply_env(default_config(), environment)

    try:
        config = from_mapping(data)
    except ConfigError as exc:
        raise ConfigError(f"{target}: {exc}") from exc

    source = str(target)
    if local_data:
        source = f"{target} + {overlay}"
    return apply_env(replace(config, source=source), environment)


def ensure_config_file(root: Path | None = None) -> Path | None:
    """Crea `config/alarm.toml` a partir del ejemplo si no existe.

    Devuelve la ruta del fichero creado, o None si ya existia o no hay ejemplo.
    El ejemplo se lee del paquete —dentro del `.exe` vive en la carpeta temporal
    de PyInstaller— y se copia a la raiz de datos, que es donde el usuario lo
    puede editar y donde la aplicacion lo va a leer despues.
    """
    base = storage_root() if root is None else Path(root)
    target = base / "config" / CONFIG_FILE.name
    if root is None:
        example = resource_path("config", EXAMPLE_NAME)
    else:
        example = base / "config" / EXAMPLE_NAME
    if target.exists() or not example.is_file():
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, target)
    except OSError as exc:  # pragma: no cover - permisos
        log.warning("no se pudo crear %s: %s", target, exc)
        return None
    log.info("configuracion creada a partir del ejemplo: %s", target)
    return target
