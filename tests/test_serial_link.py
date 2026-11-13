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
