"""Protocolo serie v1: modelos y codec."""

from .codec import decode_line, encode_ack_events, encode_cmd
from .messages import (
    CalibDone,
    CalibEdge,
    CalibWindow,
    CommandAck,
    DeviceError,
    Health,
    Heartbeat,
    Hello,
    Message,
    MotionEvent,
    MotionKind,
    ProtocolError,
    Ready,
    Zone,
)

__all__ = [
    "CalibDone",
    "CalibEdge",
    "CalibWindow",
    "CommandAck",
    "DeviceError",
    "Health",
    "Heartbeat",
    "Hello",
    "Message",
    "MotionEvent",
    "MotionKind",
    "ProtocolError",
    "Ready",
    "Zone",
    "decode_line",
    "encode_ack_events",
    "encode_cmd",
]
