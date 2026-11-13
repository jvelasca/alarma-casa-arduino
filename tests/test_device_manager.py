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
    '{"t":"hello","proto":1,"dev":"Arduino Mega 2560","fw":"1.7.0","boot":1000,'
    '"boot_id":1,"zones":[{"id":1,"name":"PIR","pin":2}],"caps":["pir"]}'
)
HB = '{"t":"hb","up":1000,"lvl":0,"raw":0,"buf":0,"lost":0}'


class FakeLink:
    """Enlace controlado por la prueba: lineas buenas o LinkError."""

    def __init__(self, *, fail_open: bool = False) -> None:
        self._open = False
        self._lines: queue.Queue[object] = queue.Queue()
        self.open_count = 0
        self.close_count = 0
        self.rejected = 0
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

    def reject_current_port(self) -> None:
        self.rejected += 1
        self.close()

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


def test_un_puerto_que_no_saluda_se_descarta_y_se_reintenta() -> None:
    """Abrir un USB ajeno no puede dejar el enlace esperando para siempre.

    Sin saludo no hay latido y el watchdog normal no actua (solo existe tras el
    primer mensaje valido): el plazo propio del handshake es el que rompe el
    bloqueo y devuelve el control para probar el siguiente candidato.
    """
    enlaces: list[Health] = []
    link = FakeLink()
    manager = DeviceManager(
        link,
        handshake_timeout_s=0.2,
        reconnect_max_s=0.1,
        on_health=enlaces.append,
    )
    manager.start()
    try:
        assert _esperar(lambda: link.rejected >= 1, timeout=2.0)
        assert manager.stats.handshake_timeouts >= 1
        assert "saludo" in manager.stats.last_error
        assert manager.health is Health.UNKNOWN
        assert Health.OFFLINE not in enlaces
        # Tras descartarlo se vuelve a abrir: no se queda pegado al primero.
        assert _esperar(lambda: link.open_count >= 2, timeout=3.0)
    finally:
        manager.stop(timeout=1.0)


def test_el_saludo_cancela_el_plazo_de_handshake() -> None:
    link = FakeLink()
    manager = DeviceManager(link, handshake_timeout_s=0.4, reconnect_max_s=0.1)
    manager.start()
    try:
        assert _esperar(lambda: link.open_count >= 1)
        link.push(HELLO)
        assert _esperar(lambda: manager.health is Health.ONLINE)
        time.sleep(0.7)
        assert link.rejected == 0
        assert manager.stats.handshake_timeouts == 0
    finally:
        manager.stop(timeout=1.0)


def test_una_reconexion_vuelve_a_exigir_el_saludo() -> None:
    link = FakeLink()
    manager = DeviceManager(link, handshake_timeout_s=0.4, reconnect_max_s=0.1)
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: manager.health is Health.ONLINE)

        # El cable se cae: la sesion nueva tampoco contesta, asi que el plazo
        # del saludo vuelve a contar y el puerto se descarta.
        link.caer()
        assert _esperar(lambda: manager.health is Health.OFFLINE)
        assert _esperar(lambda: link.rejected >= 1, timeout=4.0)
    finally:
        manager.stop(timeout=1.0)


# ---------------------------------------------------------------------------
# Acuse de eventos: diferido, acumulativo y sin duplicados
# ---------------------------------------------------------------------------

MOTION_1 = '{"t":"evt","kind":"motion.start","seq":1,"z":1,"ms":1000}'
MOTION_1_OTRA_VEZ = '{"t":"evt","kind":"motion.start","seq":1,"z":1,"ms":1000}'
MOTION_2 = '{"t":"evt","kind":"motion.start","seq":2,"z":1,"ms":2000}'
HELLO_REINICIO = (
    '{"t":"hello","proto":1,"dev":"Arduino Mega 2560","fw":"1.7.0","boot":0,'
    '"boot_id":2,"zones":[{"id":1,"name":"PIR","pin":2}],"caps":["pir"]}'
)


def _acks(link: FakeLink) -> list[str]:
    return [line for line in link.written if line.startswith("ackevt")]


def _replays(link: FakeLink) -> list[str]:
    return [line for line in link.written if line.endswith(" replay")]


def _motions(recibidos: list[object]) -> list[object]:
    from alarm.protocol.messages import MotionEvent

    return [item for item in recibidos if isinstance(item, MotionEvent)]


def test_sin_auto_ack_el_evento_no_se_confirma_al_recibirlo() -> None:
    """Recibir no es procesar: el ACK no puede adelantarse al consumidor."""
    recibidos: list[object] = []
    link = FakeLink()
    manager = DeviceManager(link, auto_ack=False, on_message=recibidos.append)
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: manager.hello is not None)
        link.push(MOTION_1)
        assert _esperar(lambda: len(_motions(recibidos)) == 1)
        time.sleep(0.15)
        assert _acks(link) == [], "el gestor no debe confirmar por su cuenta"
        assert manager.last_seq_acked == 0

        manager.ack_events(1)
        assert _esperar(lambda: _acks(link) == ["ackevt 1"])
    finally:
        manager.stop(timeout=1.0)


def test_el_ack_es_acumulativo_y_no_repite_confirmaciones() -> None:
    link = FakeLink()
    manager = DeviceManager(link, auto_ack=False)
    manager.start()
    try:
        assert _esperar(lambda: link.open_count >= 1)
        manager.ack_events(5)
        manager.ack_events(3)  # menor: ya esta confirmado
        manager.ack_events(5)  # repetido: no se escribe dos veces
        assert _esperar(lambda: _acks(link) == ["ackevt 5"])
        manager.ack_events(6)
        assert _esperar(lambda: _acks(link) == ["ackevt 5", "ackevt 6"])
    finally:
        manager.stop(timeout=1.0)


def test_confirmar_con_el_enlace_cerrado_no_bloquea_ni_lanza() -> None:
    manager = DeviceManager(FakeLink())
    manager.ack_events(4)
    assert manager.last_seq_acked == 4
    manager.ack_events(2)
    assert manager.last_seq_acked == 4, "el acuse nunca retrocede"


def test_un_evento_reenviado_no_se_entrega_dos_veces() -> None:
    recibidos: list[object] = []
    link = FakeLink()
    manager = DeviceManager(link, auto_ack=True, on_message=recibidos.append)
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: manager.hello is not None)
        link.push(MOTION_1)
        link.push(MOTION_1_OTRA_VEZ)
        assert _esperar(lambda: manager.stats.duplicates == 1)
        assert len(_motions(recibidos)) == 1, "el consumidor solo ve el evento una vez"
        assert manager.stats.events_rx == 2
    finally:
        manager.stop(timeout=1.0)


def test_al_saludar_se_pide_el_replay_una_vez_por_sesion() -> None:
    link = FakeLink()
    manager = DeviceManager(link)
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: len(_replays(link)) == 1)
        link.push(HELLO)  # un hello repetido no vuelve a pedir el buffer
        time.sleep(0.2)
        assert len(_replays(link)) == 1
    finally:
        manager.stop(timeout=1.0)


def test_un_reinicio_de_la_placa_reinicia_la_numeracion() -> None:
    recibidos: list[object] = []
    link = FakeLink()
    manager = DeviceManager(link, auto_ack=True, on_message=recibidos.append)
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: manager.hello is not None)
        link.push(MOTION_1)
        assert _esperar(lambda: manager.last_seq_acked == 1)

        link.push(HELLO_REINICIO)
        assert _esperar(lambda: manager.last_seq_acked == 0)
        # El primer evento de la placa reiniciada repite seq=1 y debe llegar.
        link.push(MOTION_1)
        assert _esperar(lambda: len(_motions(recibidos)) == 2)
    finally:
        manager.stop(timeout=1.0)


def test_reabrir_el_puerto_no_reinicia_el_acuse_hasta_ver_el_saludo() -> None:
    """Reabrir no basta: el acuse se reinicia cuando el `boot_id` es otro.

    Reiniciarlo al abrir descartaba eventos si el adaptador no pulsaba DTR y la
    placa seguia en la misma sesion; conservarlo los descartaba al reves.
    """
    recibidos: list[object] = []
    link = FakeLink()
    manager = DeviceManager(
        link,
        auto_ack=True,
        reconnect_max_s=0.1,
        hb_timeout_s=1.0,
        on_message=recibidos.append,
    )
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: manager.hello is not None)
        link.push(MOTION_2)
        assert _esperar(lambda: manager.last_seq_acked == 2)
        assert _esperar(lambda: len(_motions(recibidos)) == 1)

        aperturas = link.open_count
        link.caer()
        assert _esperar(lambda: link.open_count > aperturas, timeout=3.0)
        # El puerto se reabre, pero la placa se presenta con la misma sesion.
        link.push(HELLO)
        time.sleep(0.2)
        assert manager.last_seq_acked == 2, "la misma sesion conserva el acuse"

        # Ahora la placa se ha reiniciado de verdad: otro `boot_id`.
        link.push(HELLO_REINICIO)
        assert _esperar(lambda: manager.last_seq_acked == 0)
        link.push(MOTION_1)
        assert _esperar(lambda: len(_motions(recibidos)) == 2)
    finally:
        manager.stop(timeout=1.0)


def test_dos_arranques_distintos_con_seq_1_se_procesan_los_dos() -> None:
    """El fallo de H-02: `seq=1` de una sesion nueva no puede descartarse."""
    recibidos: list[object] = []
    link = FakeLink()
    manager = DeviceManager(link, auto_ack=True, on_message=recibidos.append)
    manager.start()
    try:
        link.push(HELLO)
        link.push(MOTION_1)
        assert _esperar(lambda: len(_motions(recibidos)) == 1)

        # Reinicio por watchdog con el puerto abierto: otro `boot_id`, seq=1.
        link.push(HELLO_REINICIO)
        assert _esperar(lambda: manager.session_boot_id == 2)
        link.push(MOTION_1)
        assert _esperar(lambda: len(_motions(recibidos)) == 2)
    finally:
        manager.stop(timeout=1.0)


def test_el_acuse_de_una_sesion_anterior_no_borra_la_nueva() -> None:
    """Si la placa se reinicia entre procesar y confirmar, el ACK viejo sobra."""
    link = FakeLink()
    manager = DeviceManager(link, auto_ack=False)
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: manager.hello is not None)
        link.push(HELLO_REINICIO)
        assert _esperar(lambda: manager.session_boot_id == 2)
        manager.ack_events(9, boot_id=1)
        time.sleep(0.2)
        assert manager.last_seq_acked == 0
        assert _acks(link) == []
    finally:
        manager.stop(timeout=1.0)


def test_un_latido_que_retrocede_delata_un_reinicio_sin_hello() -> None:
    """El watchdog reinicia la placa sin cerrar el puerto: `up` vuelve a cero."""
    link = FakeLink()
    manager = DeviceManager(link, auto_ack=True)
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: manager.hello is not None)
        link.push('{"t":"hb","up":90000,"lvl":0,"raw":0,"buf":0,"lost":0}')
        link.push(MOTION_2)
        assert _esperar(lambda: manager.last_seq_acked == 2)

        link.push('{"t":"hb","up":40,"lvl":0,"raw":0,"buf":0,"lost":0}')
        assert _esperar(lambda: manager.last_seq_acked == 0)
        # El gestor pide un `hello` para conocer el `boot_id` de la sesion.
        assert _esperar(lambda: any(line.endswith(" hello") for line in link.written))
        assert manager.stats.reboots >= 1
    finally:
        manager.stop(timeout=1.0)


def test_el_saludo_del_watchdog_se_anota_como_motivo() -> None:
    """`cfg.wdt_rst` es lo unico que distingue un cuelgue de un tiron de cable."""
    hello_wdt = (
        '{"t":"hello","proto":1,"dev":"Arduino Mega 2560","fw":"1.7.0","boot":0,'
        '"boot_id":5,"zones":[{"id":1,"name":"PIR","pin":2}],"caps":["pir"],'
        '"cfg":{"wdt_rst":1}}'
    )
    link = FakeLink()
    manager = DeviceManager(link, auto_ack=True)
    manager.start()
    try:
        link.push(HELLO)
        assert _esperar(lambda: manager.session_boot_id == 1)
        link.push(hello_wdt)
        assert _esperar(lambda: manager.session_boot_id == 5)
        assert manager.last_reboot_reason == "watchdog"
        assert manager.stats.reboots >= 1

        # Un arranque normal (sin `wdt_rst`) se anota como corte de alimentacion.
        link.push(HELLO_REINICIO)
        assert _esperar(lambda: manager.session_boot_id == 2)
        assert manager.last_reboot_reason == "power"
    finally:
        manager.stop(timeout=1.0)


def test_un_evento_que_llega_antes_del_hello_no_se_pierde() -> None:
    """Un reset por watchdog puede escupir el evento antes que el saludo.

    Sin identidad no se puede deduplicar, asi que el gestor lo aparca y lo
    entrega cuando llega el `hello`, ya con el `boot_id` de la sesion.
    """
    recibidos: list[object] = []
    link = FakeLink()
    manager = DeviceManager(link, auto_ack=True, on_message=recibidos.append)
    manager.start()
    try:
        link.push(MOTION_1)
        # El evento no se entrega sin sesion, pero tampoco se tira.
        time.sleep(0.2)
        assert _motions(recibidos) == []
        assert manager.stats.dropped_before_hello == 0

        link.push(HELLO)
        assert _esperar(lambda: len(_motions(recibidos)) == 1)
        evento = _motions(recibidos)[0]
        assert getattr(evento, "boot_id", None) == 1, "el gestor inyecta la sesion"
        assert _esperar(lambda: manager.last_seq_acked == 1)
    finally:
        manager.stop(timeout=1.0)


def test_un_firmware_sin_boot_id_se_marca_como_no_fiable() -> None:
    """1.6.0 funciona, pero la deduplicacion entre sesiones no se promete."""
    hello_antiguo = (
        '{"t":"hello","proto":1,"dev":"Arduino Mega 2560","fw":"1.6.0","boot":0,'
        '"zones":[{"id":1,"name":"PIR","pin":2}],"caps":["pir"]}'
    )
    recibidos: list[object] = []
    link = FakeLink()
    manager = DeviceManager(link, auto_ack=True, on_message=recibidos.append)
    manager.start()
    try:
        link.push(hello_antiguo)
        assert _esperar(lambda: manager.hello is not None)
        assert manager.session_boot_id is None
        assert manager.stats.legacy_sessions == 1

        link.push(MOTION_2)
        assert _esperar(lambda: manager.last_seq_acked == 2)
        # El evento viaja sin identidad: el historial no apunta la clave.
        assert getattr(_motions(recibidos)[0], "boot_id", 1) is None

        # Un `hello` repetido de la misma sesion no reinicia el acuse.
        link.push(hello_antiguo)
        time.sleep(0.2)
        assert manager.last_seq_acked == 2
        assert manager.stats.legacy_sessions == 1
    finally:
        manager.stop(timeout=1.0)
