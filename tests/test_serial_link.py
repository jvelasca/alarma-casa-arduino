"""Pruebas de la reapertura del puerto serie.

El caso que motivo estos cambios: la placa desaparece de ``COM3`` y vuelve en
``COM7``. Antes, `SerialLink` se quedaba con el primer nombre y reintentaba
contra un puerto que ya no existia para siempre. Aqui se comprueba que se
vuelve a resolver por VID/PID en cada apertura.
"""

from __future__ import annotations

import pytest

from alarm.transport import serial_link
from alarm.transport.base import LinkError
from alarm.transport.serial_link import SerialLink


class FakeSerial:
    """Minimo de `serial.Serial` que usa el enlace."""

    instances: list[FakeSerial] = []

    def __init__(self, *, port: str, baudrate: int, timeout: float, write_timeout: float) -> None:
        self.port = port
        self.baudrate = baudrate
        self.is_open = True
        FakeSerial.instances.append(self)

    def reset_input_buffer(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


@pytest.fixture(autouse=True)
def serial_falso(monkeypatch: pytest.MonkeyPatch):
    """Evita abrir puertos de verdad y limpia el registro entre pruebas."""
    FakeSerial.instances = []
    monkeypatch.setattr(serial_link.time, "sleep", lambda _seconds: None)
    yield
    FakeSerial.instances = []


def _instalar_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    import serial

    monkeypatch.setattr(serial, "Serial", FakeSerial)


def test_sin_puerto_usa_el_nombre_auto() -> None:
    link = SerialLink()
    assert link.name == "<auto>"
    assert link.port is None
    link_auto = SerialLink(port="auto")
    assert link_auto.port is None, "«auto» y «vacío» significan lo mismo"


def test_redetecta_el_puerto_cuando_el_guardado_desaparece(monkeypatch: pytest.MonkeyPatch) -> None:
    _instalar_serial(monkeypatch)
    monkeypatch.setattr(serial_link, "list_candidates", lambda: ["COM7"])
    monkeypatch.setattr(SerialLink, "_port_available", staticmethod(lambda _port: False))

    link = SerialLink(port="COM3")
    link.open()

    assert FakeSerial.instances[0].port == "COM7"
    assert link.port == "COM7"
    assert link.is_open is True


def test_conserva_el_puerto_si_sigue_existiendo(monkeypatch: pytest.MonkeyPatch) -> None:
    _instalar_serial(monkeypatch)
    monkeypatch.setattr(
        serial_link, "list_candidates", lambda: pytest.fail("no deberia redetectar")
    )
    monkeypatch.setattr(SerialLink, "_port_available", staticmethod(lambda _port: True))

    link = SerialLink(port="COM9")
    link.open()
    assert link.port == "COM9"


def test_sin_placas_el_error_lo_dice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serial_link, "list_candidates", lambda: [])
    monkeypatch.setattr(SerialLink, "_port_available", staticmethod(lambda _port: True))

    link = SerialLink()
    with pytest.raises(LinkError, match="no se detecto ninguna placa"):
        link.open()


def test_un_fallo_de_apertura_olvida_el_puerto_autodetectado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import serial

    monkeypatch.setattr(serial_link, "list_candidates", lambda: ["COM4"])

    def falla(**_kwargs: object) -> FakeSerial:
        raise serial.SerialException("acceso denegado")

    monkeypatch.setattr(serial, "Serial", falla)

    link = SerialLink()
    with pytest.raises(LinkError, match="COM4"):
        link.open()
    assert link.port is None, "el siguiente intento debe volver a buscar por VID/PID"


def test_un_fallo_de_apertura_conserva_el_puerto_elegido_por_el_usuario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import serial

    monkeypatch.setattr(SerialLink, "_port_available", staticmethod(lambda _port: True))

    def falla(**_kwargs: object) -> FakeSerial:
        raise serial.SerialException("ocupado")

    monkeypatch.setattr(serial, "Serial", falla)

    link = SerialLink(port="COM9")
    with pytest.raises(LinkError, match="COM9"):
        link.open()
    assert link.port == "COM9"


def test_cerrar_limpia_el_estado(monkeypatch: pytest.MonkeyPatch) -> None:
    _instalar_serial(monkeypatch)
    monkeypatch.setattr(serial_link, "list_candidates", lambda: ["COM2"])

    link = SerialLink()
    link.open()
    assert link.is_open is True
    link.close()
    assert link.is_open is False


# ---------------------------------------------------------------------------
# Deteccion de candidatos y rotacion tras un handshake fallido
# ---------------------------------------------------------------------------


class _Port:
    """Lo unico que `list_candidates` mira de `list_ports.comports()`."""

    def __init__(
        self, device: str, vid: int | None, pid: int | None, description: str = ""
    ) -> None:
        self.device = device
        self.vid = vid
        self.pid = pid
        self.description = description


def test_el_fallback_descarta_puertos_sin_identidad(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un puerto sin PID ni descripcion no es una placa y no debe elegirse.

    Antes se aceptaba cualquier puerto con VID, asi que un adaptador virtual
    podia quedar el primero y monopolizar la autodeteccion.
    """
    from serial.tools import list_ports

    monkeypatch.setattr(
        list_ports,
        "comports",
        lambda: [
            _Port("COM1", None, None, ""),
            _Port("COM3", 0x1234, None, "USB Serial"),
            _Port("COM5", 0x1234, 0x5678, ""),
            _Port("COM9", 0x1234, 0x5678, "USB Serial"),
        ],
    )
    assert serial_link.list_candidates() == ["COM9"]


def test_un_vid_conocido_se_prefiere_aunque_no_tenga_descripcion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from serial.tools import list_ports

    monkeypatch.setattr(
        list_ports,
        "comports",
        lambda: [
            _Port("COM4", 0x1234, 0x5678, "USB Serial"),
            _Port("COM7", 0x1A86, 0x7523, ""),
        ],
    )
    assert serial_link.list_candidates() == ["COM7"]


def test_un_puerto_descartado_se_salta_en_la_siguiente_apertura(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _instalar_serial(monkeypatch)
    monkeypatch.setattr(serial_link, "list_candidates", lambda: ["COM1", "COM2"])

    link = SerialLink()
    link.open()
    assert link.port == "COM1"

    link.reject_current_port()
    assert link.port is None
    assert link.is_open is False

    link.open()
    assert link.port == "COM2"
    assert FakeSerial.instances[-1].port == "COM2"


def test_un_puerto_fijado_por_el_usuario_no_se_descarta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si el usuario eligio COM9, un fallo de saludo no lo cambia por otro."""
    _instalar_serial(monkeypatch)
    monkeypatch.setattr(SerialLink, "_port_available", staticmethod(lambda _port: True))
    monkeypatch.setattr(serial_link, "list_candidates", lambda: ["COM1"])

    link = SerialLink(port="COM9")
    link.open()
    link.reject_current_port()
    assert link.port == "COM9"

    link.open()
    assert link.port == "COM9"
    assert FakeSerial.instances[-1].port == "COM9"


def test_el_enfriamiento_de_un_puerto_caduca(monkeypatch: pytest.MonkeyPatch) -> None:
    _instalar_serial(monkeypatch)
    monkeypatch.setattr(serial_link, "list_candidates", lambda: ["COM1"])

    link = SerialLink()
    link.open()
    link.reject_current_port()
    assert link.port is None

    # El plazo ya paso: un fallo transitorio no puede dejar la placa perdida.
    link._rejected["COM1"] = 0.0
    link.open()
    assert link.port == "COM1"


def test_si_todos_los_candidatos_estan_descartados_el_error_lo_dice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _instalar_serial(monkeypatch)
    monkeypatch.setattr(serial_link, "list_candidates", lambda: ["COM1"])

    link = SerialLink()
    link.open()
    link.reject_current_port()

    with pytest.raises(LinkError, match="no se detecto ninguna placa"):
        link.open()
