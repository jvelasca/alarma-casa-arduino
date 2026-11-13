"""Puerto de enlace serie.

Cualquier implementacion (real o simulada) cumple este contrato, de modo que
el dispositivo y las capas superiores no saben con que estan hablando.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class LinkError(RuntimeError):
    """Fallo de enlace recuperable: hay que reabrir."""


@runtime_checkable
class Link(Protocol):
    @property
    def is_open(self) -> bool: ...

    @property
    def name(self) -> str: ...

    def open(self) -> None: ...

    def close(self) -> None: ...

    def read_line(self, timeout: float) -> str | None: ...

    def write_line(self, text: str) -> None: ...
