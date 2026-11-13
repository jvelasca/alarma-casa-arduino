"""Orquestacion de los avisos remotos.

El notificador es el unico sitio que sabe que hay varios canales. Entrega en un
hilo propio y con cola acotada, porque SMTP puede tardar segundos y el hilo que
detecta la intrusion no debe esperar.

Los limites (enfriamiento y tope por hora) viven en `core.state.AlarmController`
y se inyectan como compuerta: una sola implementacion de esa regla, probada una
sola vez.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .base import Channel, DeliveryResult, sanitize

log = logging.getLogger(__name__)

Gate = Callable[[str], bool]
ResultSink = Callable[[DeliveryResult, str], None]


@dataclass(frozen=True, slots=True)
class Notification:
    cause: str
    subject: str
    body: str


class Notifier:
    def __init__(
        self,
        channels: Sequence[Channel],
        *,
        gate: Gate | None = None,
        on_result: ResultSink | None = None,
        max_queue: int = 16,
    ) -> None:
        self._channels = list(channels)
        self._gate = gate if gate is not None else (lambda _cause: True)
        self._on_result = on_result
        self._queue: queue.Queue[Notification] = queue.Queue(maxsize=max(1, max_queue))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.sent = 0
        self.failed = 0
        self.blocked = 0
        self.dropped = 0

    # -- Consulta ----------------------------------------------------------

    @property
    def available(self) -> list[str]:
        return [channel.name for channel in self._channels if channel.enabled]

    @property
    def active(self) -> bool:
        return bool(self.available)

    def channels_for(self, cause: str) -> list[str]:
        """Canales que atenderian este evento con la configuracion actual.

        Se exigen las tres condiciones: activo, configurado y que ese evento le
        toque. Un canal activo pero sin credenciales no debe aparecer como
        «en cola» para fallar diez segundos despues.
        """
        return [
            channel.name
            for channel in self._channels
            if channel.enabled and channel.configured and channel.enabled_for(cause)
        ]

    def reconfigure(self, channels: Sequence[Channel]) -> None:
        """Sustituye los canales sin reiniciar el hilo ni perder la cola.

        Se usa cuando el usuario guarda desde el dialogo de avisos: los objetos
        de canal son los mismos, ya reconfigurados; esto refresca la lista.
        """
        self._channels = list(channels)

    # -- API ---------------------------------------------------------------

    def submit(self, cause: str, subject: str, body: str, *, ignore_gate: bool = False) -> bool:
        """Encola un aviso. Devuelve False si no hay canal o si se bloqueo.

        `ignore_gate` se usa para las pruebas manuales del dialogo: el usuario
        pide el envio, asi que ni el enfriamiento ni el tope horario aplican.
        """
        if not self.channels_for(cause):
            return False
        if not ignore_gate and not self._gate(cause):
            self.blocked += 1
            log.info("aviso '%s' bloqueado por enfriamiento o limite horario", cause)
            return False

        self._ensure_thread()
        try:
            self._queue.put_nowait(Notification(cause, subject, body))
        except queue.Full:
            self.dropped += 1
            log.warning("cola de avisos llena: se descarta '%s'", cause)
            return False
        return True

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    # -- Interno -----------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._deliver(item)

    def _deliver(self, item: Notification) -> None:
        delivered = 0
        attempted = 0
        for channel in self._channels:
            if not channel.enabled or not channel.configured or not channel.enabled_for(item.cause):
                continue
            attempted += 1
            try:
                result = channel.send(item.subject, item.body, cause=item.cause)
            except Exception as exc:  # noqa: BLE001 - un canal no debe tumbar el hilo
                result = DeliveryResult(channel.name, False, sanitize(exc, ""))

            if result.ok:
                delivered += 1
            self._report(result, item.cause)

        if delivered:
            self.sent += 1
        elif attempted:
            self.failed += 1
            log.warning("no se pudo entregar el aviso '%s' por ningun canal", item.cause)

    def _report(self, result: DeliveryResult, cause: str) -> None:
        log.info("aviso '%s' por %s: %s %s", cause, result.channel, result.status, result.detail)
        if self._on_result is None:
            return
        try:
            self._on_result(result, cause)
        except Exception:  # noqa: BLE001 - el consumidor no puede romper el envio
            log.exception("fallo al notificar el resultado del aviso")
