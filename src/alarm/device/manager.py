"""Gestor del dispositivo: handshake, acuses, salud y reconexion."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field

from ..protocol.codec import decode_line, encode_ack_events, encode_cmd
from ..protocol.messages import (
    CommandAck,
    Health,
    Heartbeat,
    Hello,
    Message,
    MotionEvent,
    ProtocolError,
)
from ..transport.base import Link, LinkError

log = logging.getLogger(__name__)

MessageHandler = Callable[[Message], None]
HealthHandler = Callable[[Health], None]


@dataclass
class Stats:
    lines_rx: int = 0
    decode_errors: int = 0
    events_rx: int = 0
    reboots: int = 0
    reconnects: int = 0
    handshake_timeouts: int = 0
    duplicates: int = 0
    last_error: str = ""

    def describe(self) -> str:
        return (
            f"lineas={self.lines_rx} errores={self.decode_errors} "
            f"eventos={self.events_rx} reconexiones={self.reconnects} "
            f"sin_saludo={self.handshake_timeouts} duplicados={self.duplicates}"
        )


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    ack: CommandAck | None = None


class DeviceManager:
    """Bucle de lectura con reconexion y contabilidad de salud.

    Un unico hilo lee el enlace. Los consumidores se suscriben con
    ``on_message`` y deben ser rapidos: si necesitan trabajo lento, lo
    encolan. Esa regla es la que impide que el TTS o la red bloqueen la
    recepcion de eventos.
    """

    def __init__(
        self,
        link: Link,
        *,
        auto_ack: bool = True,
        hb_ms: int = 2000,
        hb_timeout_s: float | None = None,
        handshake_timeout_s: float = 3.0,
        reconnect_max_s: float = 30.0,
        on_message: MessageHandler | None = None,
        on_health: HealthHandler | None = None,
    ) -> None:
        self._link = link
        self._auto_ack = auto_ack
        self._hb_ms = hb_ms
        self._hb_timeout = (
            hb_timeout_s if hb_timeout_s is not None else max(6.0, hb_ms * 4 / 1000.0)
        )
        # Plazo propio del saludo. El watchdog de datos solo existe despues del
        # primer mensaje valido: sin este, un puerto que abre pero no contesta
        # (otro USB serie, un adaptador ajeno) se quedaba elegido para siempre.
        self._handshake_timeout = max(0.5, float(handshake_timeout_s))
        self._reconnect_max = reconnect_max_s

        self.on_message = on_message
        self.on_health = on_health

        self.stats = Stats()
        self.hello: Hello | None = None
        self.last_seq_acked = 0

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._opened_once = False
        # Acuse acumulativo pendiente de escribir. `ack_events` lo apunta desde
        # el hilo que consume (Qt) y el hilo lector lo escribe: la interfaz no
        # toca el puerto y el ACK sale en la siguiente vuelta del bucle.
        self._ack_target = 0
        self._ack_dirty = False
        self._replay_pending = False

        self._last_hb: float | None = None
        self._last_rx: float | None = None
        self._health = Health.UNKNOWN
        self._offline_logged = False
        self._connected = threading.Event()
        self._ever_online = False
        self._next_retry_at: float | None = None
        # Instante de la apertura en curso y si ya llego algo valido desde
        # entonces. Ambos se reinician en cada apertura: el plazo del saludo
        # se cuenta por sesion, no desde que arranco el programa.
        self._opened_at: float | None = None
        self._session_hello = False
        self._replay_done = False

    # -- Ciclo de vida -----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("el gestor ya esta arrancado")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="device-reader", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        with suppress(Exception):
            self._link.close()
        self._next_retry_at = None
        self._opened_at = None
        # Una parada pedida por el usuario no es una averia: se vuelve a
        # UNKNOWN sin avisar a la interfaz de una transicion a OFFLINE.
        self._health = Health.UNKNOWN
        self._offline_logged = False
        self._connected.clear()

    def wait_connected(self, timeout: float = 10.0) -> bool:
        """Espera a recibir el primer ``hello``."""
        return self._connected.wait(timeout)

    # -- Salud -------------------------------------------------------------

    @property
    def alive(self) -> bool:
        """El hilo de lectura sigue vivo (no ha muerto por una excepcion)."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def link(self) -> Link:
        """Enlace que se esta leyendo. La ventana lo reutiliza al relanzar."""
        return self._link

    @property
    def ever_online(self) -> bool:
        """Se recibio al menos una linea valida desde que se creo el gestor.

        Es lo que distingue «la placa se ha caido» de «la placa no estaba
        enchufada al arrancar»: solo lo primero merece un aviso.
        """
        return self._ever_online

    @property
    def next_retry_in(self) -> float | None:
        """Segundos hasta el proximo intento de reapertura, o ``None``."""
        if self._next_retry_at is None:
            return None
        return max(0.0, self._next_retry_at - time.monotonic())

    @property
    def health(self) -> Health:
        if self._health in (Health.OFFLINE, Health.UNKNOWN):
            return self._health
        if self._last_rx is None:
            return Health.UNKNOWN
        silent = time.monotonic() - self._last_rx
        if silent > self._hb_timeout:
            # Ni siquiera llegan eventos: el enlace esta muerto o el
            # dispositivo colgado.
            return Health.OFFLINE
        if silent > self._hb_timeout * 0.6:
            # Llega algo, pero menos de lo esperado.
            return Health.DEGRADED
        return Health.ONLINE

    @property
    def heartbeat_age_s(self) -> float | None:
        if self._last_hb is None:
            return None
        return time.monotonic() - self._last_hb

    def _set_health(self, value: Health) -> None:
        if value == self._health:
            return
        if value is Health.OFFLINE and not self._ever_online:
            # Antes del primer hello, un enlace que no abre no es una averia:
            # puede que la placa aun no este enchufada. Se deja UNKNOWN para
            # que la ventana no anuncie «sin sensor» al arrancar sin hardware.
            log.debug("enlace no disponible y nunca hubo datos: se mantiene UNKNOWN")
            return
        previous = self._health
        self._health = value
        if value is Health.OFFLINE and not self._offline_logged:
            self._offline_logged = True
            log.warning(
                "enlace sin datos desde hace %.1f s",
                (time.monotonic() - self._last_rx) if self._last_rx else 0.0,
            )
        elif value is Health.ONLINE:
            if self._offline_logged:
                log.info("enlace recuperado")
            self._offline_logged = False
        if self.on_health is not None:
            try:
                self.on_health(value)
            except Exception:  # pragma: no cover
                log.exception("fallo en el manejador de salud")
        log.debug("salud: %s -> %s", previous.value, value.value)

    # -- Comandos ----------------------------------------------------------

    def send(self, op: str, **params: object) -> int:
        with self._lock:
            cmd_id = self._next_id
            self._next_id += 1
            self._pending[cmd_id] = _Pending()
            line = encode_cmd(cmd_id, op, **params)
        self._link.write_line(line)
        log.debug("cmd %s", line)
        return cmd_id

    def wait_ack(self, cmd_id: int, timeout: float = 2.0) -> CommandAck | None:
        with self._lock:
            pending = self._pending.get(cmd_id)
        if pending is None:
            return None
        answered = pending.event.wait(timeout)
        with self._lock:
            self._pending.pop(cmd_id, None)
        return pending.ack if answered else None

    def request_hello(self, timeout: float = 2.0) -> Hello | None:
        self.send("hello")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.hello is not None and self._connected.is_set():
                return self.hello
            time.sleep(0.02)
        return self.hello

    def ack_events(self, seq: int) -> None:
        """Confirma eventos hasta ``seq``. Seguro desde cualquier hilo.

        El ACK solo debe llegar cuando el consumidor ha procesado el evento
        (la ventana lo hace despues de persistirlo). Aqui solo se apunta el
        numero: escribirlo es cosa del hilo lector, asi la interfaz no toca el
        puerto y dos hilos no se pisan el mismo enlace.
        """
        if seq <= 0:
            return
        with self._lock:
            if seq <= self.last_seq_acked:
                return
            # Monotono y acumulativo: confirmar N confirma todo lo anterior.
            self.last_seq_acked = seq
            self._ack_target = seq
            self._ack_dirty = True

    # -- Bucle interno -----------------------------------------------------

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if not self._link.is_open:
                    self._link.open()
                    self._opened_at = time.monotonic()
                    self._session_hello = False
                    self._replay_done = False
                    # Cada apertura es una sesion nueva. El firmware numera sus
                    # eventos desde cero tras un reinicio (que es lo que provoca
                    # abrir el puerto por DTR), asi que conservar el acuse viejo
                    # descartaria los primeros eventos de la placa. Su `boot` es
                    # casi siempre 0, de modo que no sirve para distinguirlo.
                    with self._lock:
                        self.last_seq_acked = 0
                        self._ack_target = 0
                        self._ack_dirty = False
                    backoff = 1.0
                    if self._opened_once:
                        self.stats.reconnects += 1
                    self._opened_once = True
                    self.send("hello")
                # Los acuses y el `replay` salen de este hilo y antes de leer:
                # la interfaz solo deja apuntado lo que ya proceso. Escribirlos
                # despues de `read_line` podria perder la linea recien leida si
                # el enlace falla justo al confirmar.
                self._flush_control()
                line = self._link.read_line(0.25)
            except LinkError as exc:
                self.stats.last_error = str(exc)
                log.warning("enlace caido: %s", exc)
                self._set_health(Health.OFFLINE)
                self._safe_close()
                if self._wait_retry(backoff):
                    break
                backoff = min(backoff * 2, self._reconnect_max)
                continue
            except Exception as exc:  # pragma: no cover - defensa
                self.stats.last_error = str(exc)
                log.exception("fallo inesperado en el lector")
                self._safe_close()
                if self._wait_retry(backoff):
                    break
                backoff = min(backoff * 2, self._reconnect_max)
                continue

            if line is None:
                if self._check_handshake():
                    if self._wait_retry(backoff):
                        break
                    backoff = min(backoff * 2, self._reconnect_max)
                    continue
                self._check_timeout()
                continue

            self.stats.lines_rx += 1
            try:
                message = decode_line(line)
            except ProtocolError as exc:
                self.stats.decode_errors += 1
                log.debug("linea descartada: %s (%r)", exc, line[:120])
                continue

            self._dispatch(message)

    def _flush_control(self) -> None:
        """Escribe el ACK pendiente y, si toca, pide el `replay`."""
        with self._lock:
            ack = self._ack_target if self._ack_dirty else 0
            replay = self._replay_pending
            self._replay_pending = False
        if ack:
            # Solo se marca limpio si no aparecio otro ACK mientras se escribia.
            self._link.write_line(encode_ack_events(ack))
            with self._lock:
                if self._ack_target == ack:
                    self._ack_dirty = False
        if replay:
            # Los eventos que la placa no habia confirmado se reenvian ahora;
            # los ya procesados se descartan por `last_seq_acked`.
            with self._lock:
                cmd_id = self._next_id
                self._next_id += 1
            self._link.write_line(encode_cmd(cmd_id, "replay"))

    def _wait_retry(self, delay: float) -> bool:
        """Espera antes del proximo intento y publica la cuenta atras.

        ``True`` si hay que terminar: la parada se pidio durante la espera.
        """
        self._next_retry_at = time.monotonic() + delay
        try:
            return self._stop.wait(delay)
        finally:
            self._next_retry_at = None

    def _check_handshake(self) -> bool:
        """Descarta la sesion si el puerto abrio pero no llego el saludo.

        Devuelve True cuando toca reintentar con otro candidato. El watchdog
        normal no cubre este caso: solo actua despues del primer mensaje
        valido, y aqui el problema es justo que no llega ninguno. Sin este
        plazo, elegir un USB serie ajeno dejaba el enlace esperando para
        siempre, sin equivocarse a la vista.
        """
        if self._opened_at is None or self._session_hello:
            return False
        if time.monotonic() - self._opened_at < self._handshake_timeout:
            return False
        name = self._link.name
        self.stats.handshake_timeouts += 1
        self.stats.last_error = (
            f"sin respuesta al saludo en {name} ({self._handshake_timeout:.0f} s)"
        )
        log.warning(
            "%s abrio pero no respondio al saludo en %.0f s: se descarta",
            name,
            self._handshake_timeout,
        )
        self._safe_close()
        with suppress(Exception):
            self._link.reject_current_port()
        return True

    def _check_timeout(self) -> None:
        if self._last_rx is None:
            return
        current = self.health
        if current is Health.OFFLINE:
            self._set_health(Health.OFFLINE)
        elif current is Health.DEGRADED:
            self._set_health(Health.DEGRADED)
        else:
            self._set_health(Health.ONLINE)

    def _safe_close(self) -> None:
        with suppress(Exception):
            self._link.close()
        self._opened_at = None
        self._connected.clear()

    def _dispatch(self, message: Message) -> None:
        # Cualquier linea valida demuestra que el enlace esta vivo.
        self._last_rx = time.monotonic()
        self._ever_online = True
        self._set_health(Health.ONLINE)

        if isinstance(message, Hello):
            # Un `hello` repetido es normal (lo pide el PC para resincronizar).
            # Solo es un reinicio real si cambio el instante de arranque.
            self._session_hello = True
            rebooted = self.hello is not None and message.boot_ms != self.hello.boot_ms
            if rebooted:
                self.stats.reboots += 1
                # La placa vuelve a numerar sus eventos desde cero: conservar el
                # acuse anterior descartaria los primeros de la sesion nueva.
                with self._lock:
                    self.last_seq_acked = 0
                    self._ack_target = 0
                    self._ack_dirty = False
                log.info("dispositivo reiniciado (boot=%d ms)", message.boot_ms)
            self.hello = message
            self._last_hb = time.monotonic()
            self._connected.set()
            # Lo que la placa no llego a confirmar (porque el PC se cerro o el
            # cable se cayo) sigue en su buffer: se pide una vez por sesion.
            with self._lock:
                if not self._replay_done:
                    self._replay_done = True
                    self._replay_pending = True
        elif isinstance(message, Heartbeat):
            self._last_hb = time.monotonic()
        elif isinstance(message, MotionEvent):
            self.stats.events_rx += 1
            with self._lock:
                already = message.seq <= self.last_seq_acked
            if already:
                # Reenvio de un evento ya procesado y confirmado. Se cuenta y
                # no se entrega: el consumidor no debe registrar dos veces.
                self.stats.duplicates += 1
                return
            if self._auto_ack:
                self.ack_events(message.seq)
        elif isinstance(message, CommandAck):
            with self._lock:
                pending = self._pending.get(message.id)
            if pending is not None:
                pending.ack = message
                pending.event.set()
            return  # el ack ya lo consume quien espera

        handler = self.on_message
        if handler is not None:
            try:
                handler(message)
            except Exception:  # pragma: no cover
                log.exception("fallo en el consumidor de mensajes")
