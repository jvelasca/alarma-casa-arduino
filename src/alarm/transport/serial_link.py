"""Enlace serie real sobre pyserial."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import suppress
from typing import Any

from .base import LinkError

log = logging.getLogger(__name__)

# Adaptadores USB-serie habituales en placas Arduino y clones.
KNOWN_USB_IDS: tuple[tuple[int, int], ...] = (
    (0x1A86, 0x7523),  # CH340
    (0x1A86, 0x5523),  # CH341
    (0x2341, 0x0042),  # Arduino Mega oficial (ATmega16U2)
    (0x2341, 0x0010),
    (0x2A03, 0x0042),  # Arduino.org
    (0x0403, 0x6001),  # FTDI
    (0x10C4, 0xEA60),  # CP210x
)


def list_candidates() -> list[str]:
    """Puertos que parecen una placa Arduino, en orden de aparicion."""
    try:
        from serial.tools import list_ports
    except ImportError as exc:  # pragma: no cover
        raise LinkError("pyserial no esta instalado") from exc

    found: list[str] = []
    for info in list_ports.comports():
        if info.vid is None or info.pid is None:
            continue
        if (info.vid, info.pid) in KNOWN_USB_IDS:
            found.append(info.device)
    if found:
        return found
    # Sin coincidencia por VID/PID: aceptar los puertos USB que al menos se
    # identifican. Un adaptador virtual sin PID ni descripcion no es una placa
    # y aceptarlo solo servia para bloquear la deteccion (ver H-03).
    return [
        info.device
        for info in list_ports.comports()
        if info.vid is not None and info.pid is not None and (info.description or "").strip()
    ]


def describe_ports() -> list[str]:
    from serial.tools import list_ports

    out = []
    for info in list_ports.comports():
        out.append(
            f"{info.device}: {info.description}"
            + (f" [VID_{info.vid:04X}&PID_{info.pid:04X}]" if info.vid else "")
        )
    return out


#: Segundos que un puerto descartado por no responder al saludo queda fuera,
#: para no volver a elegirlo en el intento siguiente. Pasado ese tiempo se
#: reintenta: un fallo de enumeracion no puede dejar la placa inalcanzable.
REJECT_COOLDOWN_S = 120.0


class SerialLink:
    """Enlace serie bloqueante con timeouts cortos.

    La reconexion no se gestiona aqui: el gestor decide cuando reabrir y con
    que retroceso, para que la politica sea una sola en todo el sistema.
    """

    def __init__(self, port: str | None = None, baudrate: int = 115200) -> None:
        if port is not None and port.strip().lower() in ("", "auto"):
            port = None
        self._port = port
        self._baudrate = baudrate
        # ``_autodetect`` distingue un puerto elegido por nosotros de uno fijado
        # por el usuario: si falla la apertura de un puerto autodetectado se
        # olvida, para volver a buscar por VID/PID en el siguiente intento.
        self._autodetect = port is None
        self._serial: Any = None
        # Puerto autodetectado que no respondio al saludo, con el instante en
        # que vuelve a ser candidato. Se prefiere no elegirlo mientras haya
        # otro: es lo que evita quedarse esperando en un USB ajeno.
        self._rejected: dict[str, float] = {}
        # La ventana escribe ordenes desde su hilo y el lector confirma
        # eventos: sin cerrojo, dos lineas podrian mezclarse en el mismo
        # `write` y el firmware descartaria la mezcla como linea invalida.
        self._write_lock = threading.Lock()
        # Buffer persistente entre llamadas. Es imprescindible: si llegan
        # varias lineas en una sola lectura, las posteriores a la primera
        # deben conservarse para la siguiente llamada.
        self._buf = bytearray()

    @property
    def name(self) -> str:
        return self._port or "<auto>"

    @property
    def port(self) -> str | None:
        return self._port

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> None:
        import serial

        self._port = self._resolve_port()

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                timeout=0.2,
                write_timeout=1.0,
            )
        except serial.SerialException as exc:
            self._serial = None
            port = self._port
            # El puerto guardado se quedo obsoleto: limpiarlo para que el
            # siguiente intento vuelva a buscar por VID/PID. Si el usuario fijo
            # un COM explicitamente, se conserva para no cambiar su eleccion.
            if self._autodetect:
                self._port = None
            raise LinkError(f"no se pudo abrir {port}: {exc}") from exc

        # El reset por DTR deja basura en el buffer: descartarla.
        self._buf.clear()
        time.sleep(0.05)
        with suppress(Exception):
            self._serial.reset_input_buffer()
        log.info("puerto abierto: %s @ %d", self._port, self._baudrate)

    def _resolve_port(self) -> str:
        """Elige puerto en cada apertura, no solo la primera vez.

        Un ``COMx`` cambia cuando la placa se mueve de USB o Windows reenumera.
        Quedarse con el primero convertia cualquier recolocacion en un bucle
        infinito de reintentos contra un puerto que ya no existe.

        Los puertos descartados por no responder al saludo se saltan mientras
        dure su enfriamiento: es lo que permite probar el siguiente candidato
        en vez de esperar indefinidamente en un USB ajeno.
        """
        if (
            self._port is not None
            and self._port_available(self._port)
            and not self._is_rejected(self._port)
        ):
            return self._port
        candidates = [port for port in list_candidates() if not self._is_rejected(port)]
        if not candidates:
            raise LinkError("no se detecto ninguna placa Arduino conectada")
        self._port = candidates[0]
        self._autodetect = True
        return self._port

    def reject_current_port(self) -> None:
        """Descarta el puerto actual porque no respondio al saludo.

        Solo afecta a los puertos elegidos por autodeteccion. Un ``COM5``
        fijado a mano se conserva aunque no conteste: puede ser una placa con
        un VID/PID que no esta en la lista, y cambiarla por otra seria peor que
        reintentar la que el usuario eligio.
        """
        port = self._port if self._autodetect else None
        self.close()
        if port is None:
            return
        self._rejected[port] = time.monotonic() + REJECT_COOLDOWN_S
        self._port = None
        log.info("puerto %s descartado %.0f s: no respondio al saludo", port, REJECT_COOLDOWN_S)

    def _is_rejected(self, port: str) -> bool:
        until = self._rejected.get(port)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self._rejected[port]
            return False
        return True

    @staticmethod
    def _port_available(port: str) -> bool:
        """``True`` si el puerto sigue existiendo.

        Sin pyserial no se puede comprobar: se devuelve ``True`` y se deja que
        ``open()`` falle con un mensaje claro.
        """
        try:
            from serial.tools import list_ports
        except ImportError:  # pragma: no cover - sin pyserial
            return True
        try:
            return any(info.device == port for info in list_ports.comports())
        except Exception:  # pragma: no cover - defensa
            return True

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None
        self._buf.clear()

    def read_line(self, timeout: float) -> str | None:
        if self._serial is None:
            raise LinkError("puerto cerrado")

        deadline = time.monotonic() + timeout
        while True:
            # Servir primero lo que ya este en el buffer persistente.
            newline = self._buf.find(b"\n")
            if newline >= 0:
                line = bytes(self._buf[:newline])
                del self._buf[: newline + 1]
                return line.decode("utf-8", errors="replace").strip()

            if len(self._buf) > 4096:
                self._buf.clear()
                raise LinkError("linea sin salto de linea demasiado larga")

            try:
                waiting = self._serial.in_waiting
                if waiting:
                    chunk = self._serial.read(waiting)
                else:
                    if time.monotonic() >= deadline:
                        return None
                    chunk = self._serial.read(1)
                    if not chunk:
                        continue
            except Exception as exc:
                raise LinkError(f"lectura fallida: {exc}") from exc

            self._buf.extend(chunk)

    def write_line(self, text: str) -> None:
        try:
            with self._write_lock:
                if self._serial is None:
                    raise LinkError("puerto cerrado")
                self._serial.write((text + "\n").encode("utf-8"))
                self._serial.flush()
        except LinkError:
            raise
        except Exception as exc:
            raise LinkError(f"escritura fallida: {exc}") from exc
