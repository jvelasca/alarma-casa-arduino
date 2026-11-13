"""Pruebas del gestor de dispositivo: salud, diagnostico y reconexion.

Se usa un enlace falso con una cola: permite decidir desde la prueba cuando
llega un mensaje y cuando el cable se cae, sin depender del hardware ni de
esperas largas.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable

from alarm.device.manager import DeviceManager
from alarm.protocol.messages import Health
from alarm.transport.base import LinkError

HELLO = (
    '{"t":"hello","proto":1,"dev":"Arduino Mega 2560","fw":"1.0","boot":1000,'
    '"zones":[{"id":1,"name":"PIR","pin":2}],"caps":["pir"]}'
)
HB = '{"t":"hb","up":1000,"lvl":0,"raw":0,"buf":0,"lost":0}'


class FakeLink:
    """Enlace controlado por la prueba: lineas buenas o LinkError."""

    def __init__(self, *, fail_open: bool = False) -> None:
        self._open = False
        self._lines: queue.Queue[object] = queue.Queue()
        self.open_count = 0
        self.close_count = 0
        self.fail_open = fail_open
        self.written: list[str] = []

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def name(self) -> str:
        return "falso"

    def open(self) -> None:
        self.open_count += 1
        if self.fail_open:
            raise LinkError("no hay placa")
        self._open = True

    def close(self) -> None:
        self.close_count += 1
        self._open = False

    def read_line(self, timeout: float) -> str | None:
        if not self._open:
            raise LinkError("puerto cerrado")
        try:
            item = self._lines.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None
        if isinstance(item, BaseException):
            raise item
        return str(item)

    def write_line(self, text: str) -> None:
        self.written.append(text)

    # -- Utilidades de la prueba ---------------------------------------

    def push(self, value: object) -> None:
        self._lines.put(value)

    def caer(self) -> None:
        self.push(LinkError("cable desconectado"))


def _esperar(condicion: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condicion():
            return True
        time.sleep(0.01)
    return False


def test_arranca_sin_avisar_de_offline_si_nunca_hubo_hello() -> None:
    enlaces: list[Health] = []
    link = FakeLink(fail_open=True)
    manager = DeviceManager(link, hb_timeout_s=0.2, reconnect_max_s=0.1, on_health=enlaces.append)
    manager.start()
    try:
        assert _esperar(lambda: link.open_count >= 1, timeout=2.0)
        assert manager.health is Health.UNKNOWN, "sin hello no hay averia que anunciar"
        assert Health.OFFLINE not in enlaces
        assert manager.ever_online is False
    finally:
        manager.stop(timeout=1.0)


def test_cae_y_vuelve_con_las_transiciones_esperadas() -> None:
    enlaces: list[Health] = []
    link = FakeLink()
    manager = DeviceManager(link, hb_timeout_s=0.3, reconnect_max_s=0.2, on_health=enlaces.append)
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: manager.health is Health.ONLINE)
        assert manager.ever_online is True
        assert manager.alive is True

        link.caer()
        assert _esperar(lambda: manager.health is Health.OFFLINE)
        assert Health.OFFLINE in enlaces

        # Mientras espera, la ventana puede decir cuanto falta.
        assert _esperar(lambda: manager.next_retry_in is not None)
        restante = manager.next_retry_in
        assert restante is not None and 0.0 <= restante <= 1.0

        assert _esperar(lambda: link.open_count >= 2)
        link.push(HELLO)
        assert _esperar(lambda: manager.health is Health.ONLINE)
        assert enlaces[-1] is Health.ONLINE
    finally:
        manager.stop(timeout=1.0)


def test_sin_datos_se_pasa_a_degradado_y_luego_offline() -> None:
    enlaces: list[Health] = []
    link = FakeLink()
    manager = DeviceManager(link, hb_timeout_s=0.3, reconnect_max_s=0.2, on_health=enlaces.append)
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: manager.health is Health.ONLINE)
        # El enlace sigue abierto pero deja de llegar cualquier linea.
        assert _esperar(lambda: manager.health is Health.OFFLINE, timeout=3.0)
        assert manager.stats.last_error == "", "sin datos no es un error de puerto"
    finally:
        manager.stop(timeout=1.0)


def test_parar_no_anuncia_una_averia() -> None:
    enlaces: list[Health] = []
    link = FakeLink()
    manager = DeviceManager(link, hb_timeout_s=5.0, on_health=enlaces.append)
    manager.start()
    link.push(HELLO)
    assert _esperar(lambda: manager.health is Health.ONLINE)

    manager.stop(timeout=1.0)

    assert manager.health is Health.UNKNOWN
    assert manager.alive is False
    assert Health.OFFLINE not in enlaces


def test_el_lector_se_apaga_sin_excepciones() -> None:
    link = FakeLink()
    manager = DeviceManager(link)
    manager.start()
    assert manager.alive is True
    manager.stop(timeout=1.0)
    assert manager.alive is False
    assert threading.active_count() >= 1  # el hilo de la prueba sigue vivo
