"""Avisos push al movil por ntfy.

ntfy es un servicio abierto de notificaciones: se publica un mensaje en un
tema y la aplicacion del telefono, suscrita a ese tema, lo muestra al instante.
No hay SMS, ni tarifa, ni tarjeta SIM, y funciona igual en Android y en iPhone.

Solo biblioteca estandar: `urllib` y `json`, como el resto de canales.

Tres decisiones que conviene no deshacer:

- Se publica por **JSON** a la raiz del servidor, no por cabeceras. En una
  cabecera HTTP no cabe un acento sin codificarlo, y el titulo lo lleva: con
  JSON el tema, el titulo y el mensaje viajan en el cuerpo y no hay sorpresas.
- El tema nunca se concatena a la URL sin normalizar. Un tema se valida al
  cargar la configuracion (`valid_ntfy_topic`), pero aqui se comprueba otra vez:
  el canal tambien se usa desde las pruebas manuales del dialogo.
- El token se sanea en los errores. Una excepcion de `urllib` puede arrastrar
  la peticion entera y el token no debe acabar en el registro.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from ..config import NTFY_PRIORITY_LEVELS, NtfyConfig, valid_ntfy_topic
from .base import DeliveryResult, HttpTransport, http_post, sanitize

log = logging.getLogger(__name__)

#: ntfy limita el mensaje a 4 KB. Se deja margen para el JSON y el titulo.
MAX_MESSAGE_CHARS = 3900

#: Etiqueta (emoji corto) que acompana a cada evento en el movil.
CAUSE_TAGS: dict[str, str] = {
    "triggered": "rotating_light",
    "device_lost": "warning",
    "armed": "lock",
    "disarmed": "unlock",
}

#: Palabra que se usa en el titulo cuando la configuracion no da otra.
CAUSE_WORDS: dict[str, str] = {
    "triggered": "DISPARADA",
    "device_lost": "sin sensor",
    "armed": "armada",
    "disarmed": "desarmada",
}


def clip_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Recorta el mensaje al limite del servidor, en una sola linea de corte."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_title(config: NtfyConfig, cause: str, subject: str) -> str:
    """Titulo de la notificacion: la plantilla, con el evento de esa causa.

    Se prefiere la palabra fija de la causa antes que el asunto del correo,
    porque `subject` repite «[Alarma]» y en la barra del movil se corta. Si la
    plantilla no trae `{evento}`, se usa tal cual: es lo que ha pedido el
    usuario.
    """
    event = CAUSE_WORDS.get(cause) or (subject or "Aviso")
    template = config.title or "[Alarma] {evento}"
    try:
        title = template.format(evento=event, hora="", causa=cause)
    except (KeyError, IndexError, ValueError):
        log.warning("plantilla de titulo ntfy invalida: %r", template)
        title = template
    return " ".join(title.split())[:120] or "Alarma"


@dataclass(slots=True)
class NtfyChannel:
    """Canal `ntfy` del notificador.

    Se comporta como el resto: `enabled` es el interruptor de la sesion,
    `configured` dice si hay datos para intentarlo y `enabled_for(cause)`
    decide que eventos le tocan.
    """

    config: NtfyConfig
    transport: HttpTransport = http_post
    name: str = "ntfy"
    _enabled: bool | None = None

    def __post_init__(self) -> None:
        self._enabled = self.config.enabled

    @property
    def enabled(self) -> bool:
        return bool(self._enabled) and self.config.enabled

    @property
    def configured(self) -> bool:
        return self.config.configured

    def enabled_for(self, cause: str) -> bool:
        return self.config.enabled_for(cause)

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def reconfigure(self, config: NtfyConfig) -> None:
        self.config = config
        self._enabled = config.enabled

    def send(self, subject: str, body: str, *, cause: str = "") -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(self.name, False, "canal deshabilitado")
        if not self.config.configured:
            return DeliveryResult(
                self.name,
                False,
                f"faltan datos: {', '.join(self.config.missing_fields)}",
            )
        if not valid_ntfy_topic(self.config.topic):
            return DeliveryResult(self.name, False, "tema de ntfy no válido")

        priority = self.config.priority_for(cause) if cause else "default"
        return publish(
            self.config,
            topic=self.config.topic,
            title=build_title(self.config, cause, subject),
            message=body,
            priority=priority,
            tags=(CAUSE_TAGS[cause],) if cause in CAUSE_TAGS else (),
            transport=self.transport,
            channel=self.name,
        )


def publish(
    config: NtfyConfig,
    *,
    topic: str,
    title: str,
    message: str,
    priority: str = "default",
    tags: Sequence[str] = (),
    transport: HttpTransport = http_post,
    channel: str = "ntfy",
) -> DeliveryResult:
    """Publica un mensaje JSON en un tema, con token y reintentos.

    Lo comparten los avisos y las respuestas del mando remoto: los dos hablan
    con la misma API y comparten los mismos errores. Recibe el tema explicito
    porque las respuestas pueden ir a otro tema distinto del configurado.
    """
    if not valid_ntfy_topic(topic):
        return DeliveryResult(channel, False, "tema de ntfy no válido")

    payload: dict[str, object] = {
        "topic": topic,
        "title": title,
        "message": clip_message(message.strip()),
        "priority": NTFY_PRIORITY_LEVELS.get(priority, 3),
    }
    clean_tags = [tag for tag in tags if tag]
    if clean_tags:
        payload["tags"] = clean_tags

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    url = f"{config.base_url}/"
    attempts = config.retries + 1
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            status, response = transport(url, data, headers, config.timeout_s)
        except Exception as exc:  # noqa: BLE001 - se reintenta cualquier fallo de red
            last_error = sanitize(exc, config.token)
            log.warning("intento %d/%d de aviso ntfy fallido: %s", attempt, attempts, last_error)
            continue
        if 200 <= status < 300:
            log.info("aviso ntfy entregado en el tema %s", topic)
            return DeliveryResult(channel, True, f"enviado al tema {topic} ({priority})")
        last_error = f"HTTP {status}: {response.strip()[:160]}"
        log.warning("intento %d/%d de aviso ntfy fallido: %s", attempt, attempts, last_error)
        # Un 4xx no mejora repitiendo: tema inexistente, token rechazado o
        # cuerpo demasiado grande. Solo se reintenta lo que puede ser red.
        if 400 <= status < 500 and status not in {408, 429}:
            break

    return DeliveryResult(channel, False, last_error or "sin respuesta del servidor")


__all__ = [
    "CAUSE_TAGS",
    "MAX_MESSAGE_CHARS",
    "NtfyChannel",
    "build_title",
    "clip_message",
    "publish",
]
