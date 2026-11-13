"""Contrato comun de los canales de aviso.

Un canal recibe un asunto y un cuerpo y devuelve si pudo entregarlo. Anadir
Telegram o SMS en el futuro es implementar este protocolo y registrarlo en el
`Notifier`; nada mas cambia.

`enabled_for(cause)` es la pieza que permite configurar por evento sin llenar
la interfaz de condiciones: cada canal decide si le toca ese aviso.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

#: (url, datos, cabeceras, timeout) -> (codigo, cuerpo)
HttpTransport = Callable[[str, bytes, dict[str, str], float], tuple[int, str]]


def http_post(url: str, data: bytes, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    """POST con `urllib`. Devuelve codigo y cuerpo como texto.

    Lo comparten el canal de SMS y el de ntfy: los dos publican JSON contra una
    API HTTP y los dos necesitan leer el cuerpo de un 4xx para explicar el fallo.
    """
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return int(exc.code), body


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    channel: str
    ok: bool
    detail: str = ""

    @property
    def status(self) -> str:
        return "enviado" if self.ok else "fallido"


class Channel(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def configured(self) -> bool: ...

    def enabled_for(self, cause: str) -> bool: ...

    def send(self, subject: str, body: str, *, cause: str = "") -> DeliveryResult: ...


def sanitize(exc: BaseException, secret: str) -> str:
    """Resumen de un error sin credenciales, en una sola linea.

    Una excepcion de red o de SMTP puede arrastrar el comando enviado; la
    contrasena o el token nunca deben acabar en el registro.
    """
    text = f"{type(exc).__name__}: {exc}"
    if secret:
        text = text.replace(secret, "***")
    return " ".join(text.split())[:300]
