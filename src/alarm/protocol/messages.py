"""Modelos del protocolo serie v1.

Todos los mensajes son inmutables y se validan al construirse. El codec
traduce entre estos objetos y las lineas de texto del enlace.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

PROTO_VERSION = 1


class ProtocolError(ValueError):
    """Linea que no cumple el esquema. Se descarta y se cuenta."""


class MotionKind(StrEnum):
    START = "motion.start"
    END = "motion.end"
    BOUNCE = "bounce"


@dataclass(frozen=True, slots=True)
class Zone:
    id: int
    name: str
    pin: int


@dataclass(frozen=True, slots=True)
class Hello:
    proto: int
    dev: str
    fw: str
    boot_ms: int
    zones: tuple[Zone, ...]
    caps: tuple[str, ...]
    cfg: Mapping[str, int] = field(default_factory=dict)
    # Identidad de la sesion. `boot_ms` vale ~0 siempre, asi que no distingue
    # un arranque de otro; `boot_id` si. `None` significa firmware antiguo
    # (1.6.0 y anteriores), que no lo envia: el PC lo trata como no fiable.
    boot_id: int | None = None


@dataclass(frozen=True, slots=True)
class Ready:
    warmup_ms: int


@dataclass(frozen=True, slots=True)
class Heartbeat:
    up_ms: int
    level: bool
    raw_edges: int
    buffered: int
    lost: int
    calibrating: bool
    warming_up: bool = False


@dataclass(frozen=True, slots=True)
class MotionEvent:
    seq: int
    kind: MotionKind
    zone: int
    ms: int
    dur_ms: int | None = None
    noise: int | None = None
    # Lo rellena el gestor con el `boot_id` de la sesion en la que se recibio
    # (la linea `evt` no lo trae para no alargar el enlace). Con el forma la
    # clave (boot_id, seq) que hace idempotente el procesamiento.
    boot_id: int | None = None


@dataclass(frozen=True, slots=True)
class CommandAck:
    id: int
    ok: bool
    op: str


@dataclass(frozen=True, slots=True)
class DeviceError:
    code: str
    msg: str


@dataclass(frozen=True, slots=True)
class CalibEdge:
    ms: int
    level: bool
    dt_ms: int


@dataclass(frozen=True, slots=True)
class CalibWindow:
    ms: int
    elapsed_ms: int
    edges: int
    high_ms: int
    duty_pct: float
    min_gap_ms: int
    max_gap_ms: int
    bounces: int


@dataclass(frozen=True, slots=True)
class CalibDone:
    elapsed_ms: int
    edges: int
    high_ms: int
    duty_pct: float
    min_gap_ms: int
    max_gap_ms: int
    gaps: int
    bounces: int


Message = (
    Hello
    | Ready
    | Heartbeat
    | MotionEvent
    | CommandAck
    | DeviceError
    | CalibEdge
    | CalibWindow
    | CalibDone
)


class Health(StrEnum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"
