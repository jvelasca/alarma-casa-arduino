"""Ordenes del movil por ntfy: interpretacion, permisos, escucha y respuestas.

El movil publica una frase en un tema de mando (`<tema>-mando`) y el PC la lee
en streaming. Aqui vive todo lo que no depende de la ventana:

- La gramatica: que palabras son ordenes y como se aceptan los acentos, las
  mayusculas y la puntuacion que teclea la gente.
- La autenticacion: clave de mando o codigo TOTP, con bloqueo tras varios
  fallos y sin gastar dos veces el mismo codigo.
- La escucha: suscripcion en streaming con cursor, deduplicacion y reconexion.
- Las respuestas: una cola aparte para que un POST lento no frene la escucha.

Nada de esto toca la interfaz, el controlador ni la base de datos. La ventana
recibe las ordenes por un callback y responde cuando ya ha hecho el trabajo:
asi el estado lo mueve un solo hilo, igual que cuando se pulsa un boton.
"""

from __future__ import annotations

import hmac
import json
import logging
import queue
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from ..config import NtfyConfig, RemoteConfig, valid_ntfy_topic
from .base import DeliveryResult, sanitize
from .ntfy_sender import publish
from .totp import STEP_S, verify_code

log = logging.getLogger(__name__)

#: Palabras que se aceptan para cada orden. Se comparan ya normalizadas (sin
#: acentos, en minusculas): `PÁNICO` y `panico` son la misma orden.
VERBS: dict[str, str] = {
    "armar": "arm",
    "activar": "arm",
    "arm": "arm",
    "desarmar": "disarm",
    "apagar": "disarm",
    "disarm": "disarm",
    "consultar": "status",
    "estado": "status",
    "status": "status",
    "silenciar": "silence",
    "callar": "silence",
    "silencio": "silence",
    "ayuda": "help",
    "help": "help",
    "panico": "panic",
    "panic": "panic",
}

#: Nombre legible de cada orden, para las respuestas y el registro.
ACTION_LABELS: dict[str, str] = {
    "arm": "Armar",
    "disarm": "Desarmar",
    "status": "Consultar",
    "silence": "Silenciar",
    "panic": "Pánico",
    "help": "Ayuda",
}

#: Prefijo de las respuestas propias. El PC tambien puede estar suscrito al tema
#: de avisos, y sin este filtro se contestaria a si mismo en bucle.
REPLY_PREFIX = "[Mando]"
REPLY_TAG = "mando-reply"

#: Puntuacion que se recorta en los extremos de una palabra. No incluye `-` ni
#: `_`: las claves generadas los llevan dentro.
_EDGE_JUNK = ",.;:!?¡¿\"'()[]{}<>«»…"
#: Segundos de tolerancia con el reloj del servidor al descartar mensajes viejos.
_CLOCK_SLACK_S = 5.0
#: Mensajes recordados para no ejecutar dos veces el mismo. ntfy puede repetir
#: al reconectar cerca del cursor.
_SEEN_MAX = 256
#: Tiempo de lectura del socket. ntfy manda un keepalive cada 45 s.
READ_TIMEOUT_S = 90.0
#: Espera entre reintentos de conexion.
_BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)


def _response_socket(response: object) -> socket.socket | None:
    """Socket real que hay debajo de la respuesta de `urllib`.

    `HTTPResponse` expone el fichero y este el `SocketIO`; el atributo `_sock`
    es un detalle de CPython, asi que si cambia se devuelve `None` y el cierre
    cae en el camino lento de siempre.
    """
    buffered = getattr(response, "fp", None)
    raw = getattr(buffered, "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if isinstance(sock, socket.socket) else None


# ---------------------------------------------------------------------------
# Gramatica y autenticacion
# ---------------------------------------------------------------------------


def normalise_token(token: str) -> str:
    """Minusculas, sin acentos y sin la puntuacion de los extremos."""
    text = unicodedata.normalize("NFKD", token)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().strip()
    while text and text[0] in _EDGE_JUNK:
        text = text[1:]
    while text and text[-1] in _EDGE_JUNK:
        text = text[:-1]
    return text


def verb_of(token: str) -> str:
    """Accion que representa una palabra, o cadena vacia si no es una orden."""
    raw = token.strip()
    if raw in {"?", "¿?"}:
        return "help"
    return VERBS.get(normalise_token(raw), "")


@dataclass(frozen=True, slots=True)
class Verdict:
    """Resultado de interpretar un mensaje del tema de mando."""

    action: str = ""
    accepted: bool = False
    reason: str = ""
    verb: str = ""
    blocked: bool = False
    #: True solo en el mensaje que agota el limite de fallos, para avisar una vez.
    locked_now: bool = False

    @property
    def is_command(self) -> bool:
        return bool(self.action)


@dataclass(frozen=True, slots=True)
class RemoteCommand:
    """Orden aceptada, tal como la recibe la ventana."""

    action: str
    message_id: str = ""
    received_at: float = 0.0
    sender: str = ""
    text: str = ""

    @property
    def label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)


class CommandAuthorizer:
    """Decide si un mensaje es una orden valida y lleva la cuenta de los fallos.

    El bloqueo se lleva aqui y no en la ventana porque tiene que sobrevivir a
    cualquier orden: si se dejara al consumidor, un mensaje sin manejador
    dejaria la puerta abierta a la fuerza bruta.
    """

    def __init__(self, remote: RemoteConfig, *, clock: Callable[[], float] = time.time) -> None:
        self._remote = remote
        self._clock = clock
        self._failures: deque[float] = deque()
        self._blocked_until = 0.0
        self._used_steps: set[int] = set()
        self._pruned_step = 0

    @property
    def remote(self) -> RemoteConfig:
        return self._remote

    def reconfigure(self, remote: RemoteConfig) -> None:
        self._remote = remote

    @property
    def blocked(self) -> bool:
        return self._clock() < self._blocked_until

    def blocked_for_s(self) -> float:
        return max(0.0, self._blocked_until - self._clock())

    def reset(self) -> None:
        self._failures.clear()
        self._blocked_until = 0.0

    def evaluate(self, text: str, *, now: float | None = None) -> Verdict:
        moment = self._clock() if now is None else float(now)
        tokens = text.split()
        found: tuple[int, str, str] | None = None
        for index, token in enumerate(tokens):
            action = verb_of(token)
            if action:
                found = (index, action, token)
                break
        if found is None:
            return Verdict(reason="no parece una orden")
        index, action, verb = found

        if not self._remote.allows_action(action):
            return Verdict(action=action, reason="esa orden está desactivada", verb=verb)

        if moment < self._blocked_until:
            return Verdict(
                action=action,
                reason="mando bloqueado por demasiados intentos",
                verb=verb,
                blocked=True,
            )

        credentials = [
            token.strip().strip(_EDGE_JUNK)
            for position, token in enumerate(tokens)
            if position != index
        ]
        credentials = [token for token in credentials if token]

        bad = self._check_credentials(credentials, moment)
        if bad is None:
            self._failures.clear()
            return Verdict(action=action, accepted=True, verb=verb)

        locked_now = self._register_failure(moment)
        return Verdict(
            action=action,
            reason=bad,
            verb=verb,
            blocked=locked_now,
            locked_now=locked_now,
        )

    # -- Interno -----------------------------------------------------------

    def _check_credentials(self, tokens: Sequence[str], moment: float) -> str | None:
        """None si la credencial es buena; si no, el motivo del rechazo."""
        remote = self._remote
        if remote.totp_secret:
            # Con TOTP configurado la clave deja de valer: si se aceptaran las
            # dos, una clave filtrada abriria la puerta igual que sin TOTP.
            self._prune_steps(moment)
            for token in tokens:
                digits = token.replace(" ", "")
                if (
                    len(digits) == 6
                    and digits.isdigit()
                    and verify_code(remote.totp_secret, digits, moment, used=self._used_steps)
                ):
                    return None
            return "código TOTP no válido o ya usado"
        if remote.key:
            for token in tokens:
                if hmac.compare_digest(token, remote.key):
                    return None
            return "clave de mando incorrecta"
        return "el mando no tiene clave configurada"

    def _prune_steps(self, moment: float) -> None:
        """Olvida los pasos gastados que ya no pueden volver a usarse."""
        current = int(moment // STEP_S)
        if current == self._pruned_step:
            return
        self._pruned_step = current
        self._used_steps = {step for step in self._used_steps if abs(step - current) <= 2}

    def _register_failure(self, moment: float) -> bool:
        """Anota un fallo. Devuelve True si este fallo acaba de bloquear."""
        limit = self._remote.lockout_failures
        if limit <= 0:
            return False
        window_s = self._remote.lockout_minutes * 60
        self._failures.append(moment)
        while self._failures and moment - self._failures[0] > window_s:
            self._failures.popleft()
        if len(self._failures) >= limit:
            self._failures.clear()
            self._blocked_until = moment + window_s
            return True
        return False


# ---------------------------------------------------------------------------
# Estado que ve la ventana
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    """Una orden vista en esta sesion, para el registro del dialogo."""

    at: float
    text: str
    result: str
    ok: bool


@dataclass(frozen=True, slots=True)
class RemoteState:
    """Fotografia del escucha, para pintar el dialogo y la barra de estado."""

    listening: bool
    connected: bool
    since: float
    topic: str
    error: str = ""
    received: int = 0
    accepted: int = 0
    rejected: int = 0
    last_text: str = ""
    last_result: str = ""
    last_at: float = 0.0
    #: Diferencia entre el reloj local y el del servidor en el último mensaje.
    #: Con TOTP importa: un desfase de más de 30 s invalida todos los códigos.
    skew_s: float = 0.0
    entries: tuple[RemoteEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class StatusInfo:
    """Datos con los que se responde a «consultar»."""

    state_label: str
    link: str
    sensor: str = ""
    armed: bool = False
    silenced: bool = False
    siren_s: float | None = None
    closing_s: float | None = None
    delay_kind: str | None = None
    delays: str = ""
    channels: tuple[str, ...] = ()
    events: tuple[str, ...] = ()
    now: str = ""


def format_status(info: StatusInfo) -> str:
    """Texto de la respuesta a «consultar», pensado para leerlo en el movil."""
    lines = [f"Estado: {info.state_label}"]
    if info.closing_s is not None and info.delay_kind:
        lines.append(f"Cuenta atrás ({info.delay_kind}): quedan {info.closing_s:.0f} s")
    if info.siren_s is not None and info.siren_s > 0:
        lines.append(f"Sirena: quedan {info.siren_s:.0f} s")
    if info.silenced:
        lines.append("Avisos: silenciada a mano, sigue armada")
    lines.append(f"Enlace: {info.link or 'sin datos'}")
    if info.sensor:
        lines.append(f"Sensor: {info.sensor}")
    if info.delays:
        lines.append(f"Retardos: {info.delays}")
    lines.append("Canales: " + (", ".join(info.channels) if info.channels else "ninguno activo"))
    if info.events:
        lines.append("")
        lines.append("Últimos eventos:")
        lines.extend(f"· {event}" for event in info.events)
    if info.now:
        lines.append("")
        lines.append(f"Hora: {info.now}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Escucha en streaming
# ---------------------------------------------------------------------------


class StreamError(RuntimeError):
    """El servidor cortó el stream; se reintenta con el mismo cursor."""


# (url, cabeceras, timeout de lectura, evento de parada) -> lineas
SubscribeFn = Callable[[str, dict[str, str], float, threading.Event], Iterator[str]]
# (tema, titulo, mensaje, prioridad, etiquetas) -> resultado
SendFn = Callable[[str, str, str, str, Sequence[str]], DeliveryResult]


class CommandListener:
    """Suscriptor del tema de mando con respuestas en un hilo aparte."""

    def __init__(
        self,
        ntfy: NtfyConfig,
        remote: RemoteConfig,
        topic: str,
        *,
        on_command: Callable[[RemoteCommand], None],
        on_state: Callable[[RemoteState], None] | None = None,
        on_event: Callable[[str, str, str], None] | None = None,
        subscribe: SubscribeFn | None = None,
        send_message: SendFn | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ntfy = ntfy
        self._remote = remote
        self._topic = topic
        self._on_command = on_command
        self._on_state = on_state
        self._on_event = on_event
        self._clock = clock
        self._authorizer = CommandAuthorizer(remote, clock=clock)
        self._subscribe = subscribe if subscribe is not None else self._open_stream
        self._send = send_message if send_message is not None else self._publish_reply

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._responder: threading.Thread | None = None
        self._outbox: queue.Queue[tuple[str, str, str, tuple[str, ...]]] = queue.Queue(maxsize=32)
        self._response: Any = None
        self._socket: socket.socket | None = None
        self._cursor = int(clock())
        self._started = clock()
        self._seen: deque[str] = deque(maxlen=_SEEN_MAX)
        self._seen_set: set[str] = set()

        self._connected = False
        self._error = ""
        self._counts = {"received": 0, "accepted": 0, "rejected": 0}
        self._last_text = ""
        self._last_result = ""
        self._last_at = 0.0
        self._skew_s = 0.0
        self._entries: deque[RemoteEntry] = deque(maxlen=20)

    # -- Consulta ----------------------------------------------------------

    @property
    def topic(self) -> str:
        return self._topic

    @property
    def authorizer(self) -> CommandAuthorizer:
        return self._authorizer

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def matches(self, ntfy: NtfyConfig, remote: RemoteConfig, topic: str) -> bool:
        """Si este listener sirve para esa configuracion, sin reiniciarlo.

        Se compara todo lo que cambia el comportamiento: servidor y token,
        credenciales, ordenes permitidas, respuestas y bloqueo. Reiniciar por
        un cambio de texto de voz seria absurdo; no reiniciar tras cambiar la
        clave dejaria el mando aceptando la anterior.
        """
        return self._topic == topic and self._ntfy == ntfy and self._remote == remote

    def snapshot(self) -> RemoteState:
        return RemoteState(
            listening=self.running,
            connected=self._connected,
            since=self._started,
            topic=self._topic,
            error=self._error,
            received=self._counts["received"],
            accepted=self._counts["accepted"],
            rejected=self._counts["rejected"],
            last_text=self._last_text,
            last_result=self._last_result,
            last_at=self._last_at,
            skew_s=self._skew_s,
            entries=tuple(self._entries),
        )

    # -- Ciclo de vida -----------------------------------------------------

    def start(self) -> None:
        if self.running:
            return
        if not valid_ntfy_topic(self._topic):
            self._error = "tema de mando no válido"
            self._emit_state()
            return
        self._stop.clear()
        self._started = self._clock()
        self._cursor = int(self._started)
        self._thread = threading.Thread(target=self._run, name="ntfy-commands", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        # Desbloquear la lectura en curso sin esperar al keepalive del servidor.
        # `response.close()` no sirve: comparte el cerrojo del lector y puede
        # quedarse esperando justo la lectura que hay que interrumpir.
        self._interrupt_stream()
        for thread in (self._thread, self._responder):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
        self._thread = None
        self._responder = None
        self._drain_outbox()
        self._connected = False
        self._emit_state()

    # -- Respuestas --------------------------------------------------------

    def reply(
        self,
        text: str,
        *,
        title: str = f"{REPLY_PREFIX} Alarma",
        priority: str = "default",
        tags: Sequence[str] = (REPLY_TAG, "white_check_mark"),
    ) -> bool:
        """Encola una respuesta para el movil. Devuelve si se pudo encolar."""
        if not self._remote.reply:
            return False
        return self._enqueue(title, text, priority, tags)

    def alert(self, title: str, text: str) -> None:
        """Aviso urgente por el tema de avisos (bloqueo del mando)."""
        self._enqueue(title, text, "high", ("warning",))

    def _enqueue(self, title: str, text: str, priority: str, tags: Sequence[str]) -> bool:
        self._ensure_responder()
        try:
            self._outbox.put_nowait((title, text, priority, tuple(tags)))
        except queue.Full:
            log.warning("cola de respuestas del mando llena: se descarta %r", title)
            return False
        return True

    def _ensure_responder(self) -> None:
        if self._responder is not None and self._responder.is_alive():
            return
        self._responder = threading.Thread(
            target=self._run_responder, name="ntfy-replies", daemon=True
        )
        self._responder.start()

    def _run_responder(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._outbox.get(timeout=0.2)
            except queue.Empty:
                continue
            title, text, priority, tags = item
            try:
                result = self._send(self._ntfy.topic, title, text, priority, tags)
            except Exception as exc:  # noqa: BLE001 - una respuesta no puede tumbar nada
                log.warning("no se pudo responder al movil: %s", sanitize(exc, self._ntfy.token))
                continue
            if not result.ok:
                log.warning("respuesta al movil rechazada: %s", result.detail)

    def _drain_outbox(self) -> None:
        while True:
            try:
                self._outbox.get_nowait()
            except queue.Empty:
                return

    def _publish_reply(
        self, topic: str, title: str, text: str, priority: str, tags: Sequence[str]
    ) -> DeliveryResult:
        return publish(
            self._ntfy,
            topic=topic,
            title=title,
            message=text,
            priority=priority,
            tags=tags,
            channel="ntfy-mando",
        )

    # -- Bucle del suscriptor ---------------------------------------------

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            url = f"{self._ntfy.base_url}/{self._topic}/json?since={self._cursor}"
            headers = {"Accept": "application/json"}
            if self._ntfy.token:
                headers["Authorization"] = f"Bearer {self._ntfy.token}"
            try:
                self._connected = True
                self._error = ""
                self._emit_state()
                for line in self._subscribe(url, headers, READ_TIMEOUT_S, self._stop):
                    if self._stop.is_set():
                        break
                    self._handle_line(line)
                attempt = 0
            except Exception as exc:  # noqa: BLE001 - cualquier fallo se reintenta
                # Cerrar el socket desde `stop()` desbloquea la lectura y puede
                # lanzar un AttributeError de `http.client`; parar no es un fallo.
                if self._stop.is_set():
                    break
                self._error = sanitize(exc, self._ntfy.token)
                log.warning("suscripción de mando interrumpida: %s", self._error)
            finally:
                self._connected = False
            if self._stop.is_set():
                break
            wait = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
            attempt += 1
            self._emit_state()
            self._stop.wait(wait)
        self._connected = False
        self._emit_state()

    def _interrupt_stream(self) -> None:
        """Corta la lectura en curso antes de cerrar la respuesta.

        Cerrar un `HTTPResponse` desde otro hilo comparte el cerrojo del lector
        y puede tardar hasta el siguiente keepalive. Un `shutdown` sobre el
        socket real despierta la lectura al instante.
        """
        sock, self._socket = self._socket, None
        if sock is not None:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        response, self._response = self._response, None
        if response is not None:
            with suppress(Exception):
                response.close()

    def _open_stream(
        self, url: str, headers: dict[str, str], timeout: float, stop: threading.Event
    ) -> Iterator[str]:
        """Suscripcion por streaming con `urllib`, sin dependencias."""
        request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
        response = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
        self._response = response
        self._socket = _response_socket(response)
        try:
            for raw in response:
                if stop.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    yield line
        finally:
            self._response = None
            self._socket = None
            with suppress(Exception):
                response.close()

    def _handle_line(self, line: str) -> None:
        try:
            data = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        event = str(data.get("event") or "")
        if event in {"open", "keepalive", "poll_request"}:
            self._connected = True
            return
        if event != "message":
            return

        identifier = str(data.get("id") or "")
        if identifier and identifier in self._seen_set:
            return
        if identifier:
            if len(self._seen) == self._seen.maxlen:
                oldest = self._seen[0]
                self._seen_set.discard(oldest)
            self._seen.append(identifier)
            self._seen_set.add(identifier)

        stamp = data.get("time")
        if isinstance(stamp, (int, float)) and f"{stamp}".strip():
            moment = float(stamp)
            if moment < self._started - _CLOCK_SLACK_S:
                return
            self._cursor = max(self._cursor, int(moment))
            self._skew_s = self._clock() - moment

        title = str(data.get("title") or "")
        tags = data.get("tags") or []
        if title.startswith(REPLY_PREFIX):
            return
        if isinstance(tags, list) and REPLY_TAG in {str(tag) for tag in tags}:
            return

        text = str(data.get("message") or "").strip()
        if not text:
            return

        self._counts["received"] += 1
        verdict = self._authorizer.evaluate(text)
        moment = self._clock()
        # El texto del mensaje puede llevar la clave o el codigo: al registro y
        # a la tabla solo va el verbo. Un historial no es sitio para secretos.
        shown = normalise_token(verdict.verb) if verdict.verb else "mensaje no reconocido"

        if verdict.accepted:
            self._counts["accepted"] += 1
            self._record(moment, shown, "aceptada", True)
            self._on_command(
                RemoteCommand(
                    action=verdict.action,
                    message_id=identifier,
                    received_at=moment,
                    text=shown,
                )
            )
        else:
            if verdict.is_command:
                self._counts["rejected"] += 1
                self._record(moment, shown, verdict.reason or "rechazada", False)
            else:
                self._record(moment, shown, "no es una orden", False)
            log.info("orden remota descartada: %s (%s)", shown, verdict.reason)
            self._emit_on_event("warning" if verdict.is_command else "info", shown, verdict.reason)

        if verdict.locked_now:
            minutes = self._remote.lockout_minutes
            self.alert(
                "[Alarma] Mando bloqueado",
                f"Demasiados intentos fallidos desde el móvil.\n\n"
                f"Se ignorarán las órdenes {minutes} min. Revisa el tema y la clave.",
            )
            self._emit_on_event(
                "error",
                "Mando bloqueado",
                f"{self._remote.lockout_failures} fallos: {minutes} min sin aceptar órdenes",
            )
        self._emit_state()

    def _record(self, at: float, text: str, result: str, ok: bool) -> None:
        self._entries.append(RemoteEntry(at=at, text=text, result=result, ok=ok))
        self._last_text = text
        self._last_result = result
        self._last_at = at

    def _emit_state(self) -> None:
        if self._on_state is None:
            return
        try:
            self._on_state(self.snapshot())
        except Exception:  # noqa: BLE001 - el consumidor no puede romper la escucha
            log.exception("fallo al informar del estado del mando remoto")

    def _emit_on_event(self, kind: str, title: str, detail: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, title, detail)
        except Exception:  # noqa: BLE001
            log.exception("fallo al registrar un evento del mando remoto")


__all__ = [
    "ACTION_LABELS",
    "CommandAuthorizer",
    "CommandListener",
    "REPLY_PREFIX",
    "REPLY_TAG",
    "READ_TIMEOUT_S",
    "RemoteCommand",
    "RemoteEntry",
    "RemoteState",
    "StatusInfo",
    "VERBS",
    "Verdict",
    "format_status",
    "normalise_token",
    "verb_of",
]
