"""Pruebas del escucha del tema de mando.

No hay red: la suscripcion y la publicacion se inyectan. Lo que se comprueba es
el contrato, que es donde estan los fallos caros: que no se ejecute nada viejo,
que no se responda dos veces, que una respuesta propia no se tome por una orden
y que la clave no acabe en el historial.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import replace

from alarm.config import NtfyConfig, RemoteConfig
from alarm.services.ntfy_commands import CommandListener, RemoteCommand
from alarm.services.ntfy_sender import DeliveryResult

BASE_TIME = 1_700_000_000.0


def ntfy_config() -> NtfyConfig:
    return NtfyConfig(
        enabled=True,
        server="https://ntfy.sh",
        topic="casa-abc123",
        token="",
        retries=0,
        timeout_s=2.0,
    )


def remote(**overrides: object) -> RemoteConfig:
    base = RemoteConfig(enabled=True, key="clave-de-prueba-1234")
    return replace(base, **overrides)  # type: ignore[arg-type]


def message(
    text: str,
    *,
    identifier: str = "m1",
    at: float = BASE_TIME,
    title: str = "",
    tags: list[str] | None = None,
) -> str:
    payload = {
        "id": identifier,
        "time": int(at),
        "event": "message",
        "topic": "casa-abc123-mando",
        "message": text,
        "title": title,
        "tags": tags or [],
    }
    return json.dumps(payload)


class FakeStream:
    """Suscripcion de mentira: entrega lotes y luego espera a que la paren."""

    def __init__(self, *batches: list[str]) -> None:
        self.batches = list(batches)
        self.urls: list[str] = []

    def __call__(
        self, url: str, headers: dict[str, str], timeout: float, stop: threading.Event
    ) -> Iterator[str]:
        self.urls.append(url)
        if self.batches:
            batch = self.batches.pop(0)
            for item in batch:
                if stop.is_set():
                    return
                yield item
            return
        # Sin mas datos: mantener la conexion hasta que pidan parar.
        stop.wait(0.5)


class FakeSend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str, tuple[str, ...]]] = []
        self.lock = threading.Lock()

    def __call__(
        self, topic: str, title: str, text: str, priority: str, tags: object
    ) -> DeliveryResult:
        with self.lock:
            self.calls.append((topic, title, text, priority, tuple(tags)))  # type: ignore[arg-type]
        return DeliveryResult("ntfy-mando", True, "ok")


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_listener(
    stream: FakeStream,
    *,
    sender: FakeSend | None = None,
    remote_config: RemoteConfig | None = None,
    on_command=None,
    on_event=None,
) -> tuple[CommandListener, FakeStream, FakeSend, list[RemoteCommand]]:
    commands: list[RemoteCommand] = []
    handler = on_command if on_command is not None else commands.append
    send = sender if sender is not None else FakeSend()
    listener = CommandListener(
        ntfy_config(),
        remote_config or remote(),
        "casa-abc123-mando",
        on_command=handler,
        on_event=on_event,
        subscribe=stream,
        send_message=send,
        clock=lambda: BASE_TIME,
    )
    return listener, stream, send, commands


# ---------------------------------------------------------------------------
# Filtros
# ---------------------------------------------------------------------------


def test_un_mensaje_anterior_al_arranque_no_se_ejecuta() -> None:
    stream = FakeStream([message("clave-de-prueba-1234 desarmar", at=BASE_TIME - 3600)])
    listener, _stream, _send, commands = make_listener(stream)
    listener.start()
    try:
        # Se le da tiempo a procesar; no debe llegar ninguna orden.
        time.sleep(0.2)
        assert commands == []
        assert listener.snapshot().accepted == 0
    finally:
        listener.stop()


def test_una_orden_valida_se_ejecuta_una_sola_vez() -> None:
    stream = FakeStream(
        [
            message("clave-de-prueba-1234 armar", identifier="a"),
            message("clave-de-prueba-1234 armar", identifier="a"),
        ]
    )
    listener, _stream, _send, commands = make_listener(stream)
    listener.start()
    try:
        assert wait_for(lambda: len(commands) == 1)
        time.sleep(0.15)
        assert [c.action for c in commands] == ["arm"]
    finally:
        listener.stop()


def test_las_respuestas_propias_se_ignoran() -> None:
    stream = FakeStream(
        [
            message("clave-de-prueba-1234 armar", identifier="1", title="[Mando] Armada"),
            message("clave-de-prueba-1234 armar", identifier="2", tags=["mando-reply"]),
        ]
    )
    listener, _stream, _send, commands = make_listener(stream)
    listener.start()
    try:
        time.sleep(0.25)
        assert commands == []
    finally:
        listener.stop()


def test_open_y_keepalive_no_ejecutan_nada() -> None:
    stream = FakeStream(
        [
            json.dumps({"event": "open", "topic": "casa-abc123-mando"}),
            json.dumps({"event": "keepalive", "topic": "casa-abc123-mando"}),
            "no es json",
        ]
    )
    listener, _stream, _send, commands = make_listener(stream)
    listener.start()
    try:
        time.sleep(0.2)
        assert commands == []
        assert listener.snapshot().received == 0
    finally:
        listener.stop()


def test_la_clave_no_se_guarda_en_el_registro() -> None:
    events: list[tuple[str, str, str]] = []
    stream = FakeStream([message("clave-mala armar")])
    listener, _stream, _send, commands = make_listener(
        stream,
        on_event=lambda *args: events.append(args),  # type: ignore[arg-type]
    )
    listener.start()
    try:
        assert wait_for(lambda: bool(events))
        assert commands == []
        joined = " ".join(part for event in events for part in event)
        assert "clave-mala" not in joined
        assert "armar" in joined
        entry = listener.snapshot().entries[-1]
        assert entry.ok is False
        assert "clave-mala" not in entry.text
    finally:
        listener.stop()


def test_una_orden_desactivada_no_ejecuta_y_queda_registrada() -> None:
    stream = FakeStream([message("clave-de-prueba-1234 armar")])
    listener, _stream, _send, commands = make_listener(
        stream, remote_config=remote(allow_arm=False)
    )
    listener.start()
    try:
        assert wait_for(lambda: listener.snapshot().rejected == 1)
        assert commands == []
    finally:
        listener.stop()


# ---------------------------------------------------------------------------
# Respuestas
# ---------------------------------------------------------------------------


def test_la_respuesta_lleva_titulo_y_etiqueta_de_mando() -> None:
    stream = FakeStream()
    listener, _stream, send, _commands = make_listener(stream)
    listener.start()
    try:
        listener.reply("Armada. Vigilando.", title="[Mando] Armar")
        assert wait_for(lambda: bool(send.calls))
        topic, title, text, priority, tags = send.calls[0]
        assert topic == "casa-abc123"  # responde en el tema de avisos
        assert title == "[Mando] Armar"
        assert "Armada" in text
        assert priority == "default"
        assert "mando-reply" in tags
    finally:
        listener.stop()


def test_sin_respuestas_configuradas_no_se_publica_nada() -> None:
    stream = FakeStream()
    listener, _stream, send, _commands = make_listener(stream, remote_config=remote(reply=False))
    listener.start()
    try:
        assert listener.reply("no deberia salir") is False
        time.sleep(0.15)
        assert send.calls == []
    finally:
        listener.stop()


def test_el_aviso_de_bloqueo_se_publica_una_sola_vez() -> None:
    sender = FakeSend()
    stream = FakeStream([message("mala armar", identifier=f"{i}") for i in range(1, 5)])
    listener, _stream, _send, _commands = make_listener(
        stream, sender=sender, remote_config=remote(lockout_failures=2)
    )
    listener.start()
    try:
        assert wait_for(lambda: len(sender.calls) >= 1)
        assert wait_for(lambda: listener.snapshot().received == 4)
        alerts = [call for call in sender.calls if "bloqueado" in call[1].lower()]
        assert len(alerts) == 1
    finally:
        listener.stop()


# ---------------------------------------------------------------------------
# Conexion
# ---------------------------------------------------------------------------


def test_la_suscripcion_empieza_en_el_instante_de_arranque() -> None:
    stream = FakeStream()
    listener, _stream, _send, _commands = make_listener(stream)
    listener.start()
    try:
        assert wait_for(lambda: bool(stream.urls))
        assert f"since={int(BASE_TIME)}" in stream.urls[0]
        assert stream.urls[0].startswith("https://ntfy.sh/casa-abc123-mando/json")
    finally:
        listener.stop()


def test_al_reconectar_se_reutiliza_el_cursor_del_ultimo_mensaje() -> None:
    later = BASE_TIME + 120
    stream = FakeStream(
        [message("clave-de-prueba-1234 armar", identifier="a", at=later)],
        [],
    )
    listener, _stream, _send, commands = make_listener(stream)
    listener.start()
    try:
        assert wait_for(lambda: len(commands) == 1)
        # El segundo intento de conexion ya usa el instante del mensaje.
        assert wait_for(lambda: len(stream.urls) >= 2, timeout=4.0)
        assert f"since={int(later)}" in stream.urls[1]
    finally:
        listener.stop()


def test_stop_para_los_hilos_y_deja_de_escuchar() -> None:
    stream = FakeStream()
    listener, _stream, _send, _commands = make_listener(stream)
    listener.start()
    assert wait_for(lambda: listener.running)
    listener.stop()
    assert listener.running is False
    assert listener.snapshot().listening is False
    assert listener.snapshot().connected is False


def test_parar_no_registra_el_error_del_socket_cerrado() -> None:
    """Al cerrar la respuesta, `http.client` puede lanzar al leer; no es un fallo."""
    called = threading.Event()

    def stream(url, headers, timeout, stop):
        called.set()
        stop.wait(2.0)
        raise AttributeError("'NoneType' object has no attribute 'peek'")

    listener, _stream, _send, _commands = make_listener(stream)
    listener.start()
    try:
        assert wait_for(called.is_set)
    finally:
        listener.stop()
    assert listener.running is False
    assert listener.snapshot().error == ""
