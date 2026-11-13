"""Codec del protocolo v1.

Uplink (dispositivo -> PC): NDJSON, una linea por objeto JSON.
Downlink (PC -> dispositivo): texto tokenizado ``cmd <id> <op> k=v ...``.

El decodificador es estricto a proposito: cualquier linea que no cumpla el
esquema lanza ProtocolError para que el llamador la descarte y la cuente.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .messages import (
    CalibDone,
    CalibEdge,
    CalibWindow,
    CommandAck,
    DeviceError,
    Heartbeat,
    Hello,
    Message,
    MotionEvent,
    MotionKind,
    ProtocolError,
    Ready,
    Zone,
)

# ---------------------------------------------------------------------------
# Validacion
# ---------------------------------------------------------------------------


def _req(d: Mapping[str, Any], key: str, types: tuple[type, ...], where: str) -> Any:
    if key not in d:
        raise ProtocolError(f"{where}: falta el campo '{key}'")
    value = d[key]
    if isinstance(value, bool) and bool not in types:
        raise ProtocolError(f"{where}: '{key}' debe ser {types[0].__name__}, no bool")
    if not isinstance(value, types):
        names = "/".join(t.__name__ for t in types)
        raise ProtocolError(f"{where}: '{key}' debe ser {names}, es {type(value).__name__}")
    return value


def _opt(d: Mapping[str, Any], key: str, types: tuple[type, ...], default: Any) -> Any:
    if key not in d:
        return default
    return _req(d, key, types, "msg")


def _int(d: Mapping[str, Any], key: str, where: str) -> int:
    return int(_req(d, key, (int,), where))


# ---------------------------------------------------------------------------
# Decodificacion
# ---------------------------------------------------------------------------


def _decode_hello(d: Mapping[str, Any]) -> Hello:
    raw_zones = _req(d, "zones", (list,), "hello")
    zones = []
    for z in raw_zones:
        if not isinstance(z, dict):
            raise ProtocolError("hello: cada zona debe ser un objeto")
        zones.append(
            Zone(
                id=_int(z, "id", "hello.zone"),
                name=str(_req(z, "name", (str,), "hello.zone")),
                pin=_int(z, "pin", "hello.zone"),
            )
        )
    caps = tuple(str(c) for c in _opt(d, "caps", (list,), []))
    cfg_raw = _opt(d, "cfg", (dict,), {})
    cfg = {str(k): int(v) for k, v in cfg_raw.items() if isinstance(v, int)}
    return Hello(
        proto=_int(d, "proto", "hello"),
        dev=str(_req(d, "dev", (str,), "hello")),
        fw=str(_req(d, "fw", (str,), "hello")),
        boot_ms=_int(d, "boot", "hello"),
        zones=tuple(zones),
        caps=caps,
        cfg=cfg,
    )


def _decode_ready(d: Mapping[str, Any]) -> Ready:
    return Ready(warmup_ms=_int(d, "warmup_ms", "ready"))


def _decode_hb(d: Mapping[str, Any]) -> Heartbeat:
    return Heartbeat(
        up_ms=_int(d, "up", "hb"),
        level=bool(_opt(d, "lvl", (int, bool), 0)),
        raw_edges=_int(d, "raw", "hb"),
        buffered=_int(d, "buf", "hb"),
        lost=_int(d, "lost", "hb"),
        calibrating=bool(_opt(d, "calib", (int, bool), 0)),
        warming_up=bool(_opt(d, "warmup", (int, bool), 0)),
    )


def _decode_evt(d: Mapping[str, Any]) -> MotionEvent:
    kind_raw = _req(d, "kind", (str,), "evt")
    try:
        kind = MotionKind(kind_raw)
    except ValueError as exc:
        raise ProtocolError(f"evt: kind desconocido '{kind_raw}'") from exc
    return MotionEvent(
        seq=_int(d, "seq", "evt"),
        kind=kind,
        zone=_int(d, "z", "evt"),
        ms=_int(d, "ms", "evt"),
        dur_ms=_opt(d, "dur", (int,), None),
        noise=_opt(d, "n", (int,), None),
    )


def _decode_ack(d: Mapping[str, Any]) -> CommandAck:
    ok = _req(d, "ok", (bool, int), "ack")
    return CommandAck(
        id=_int(d, "id", "ack"),
        ok=bool(ok),
        op=str(_opt(d, "op", (str,), "")),
    )


def _decode_err(d: Mapping[str, Any]) -> DeviceError:
    return DeviceError(
        code=str(_req(d, "code", (str,), "err")),
        msg=str(_opt(d, "msg", (str,), "")),
    )


def _decode_calib_edge(d: Mapping[str, Any]) -> CalibEdge:
    return CalibEdge(
        ms=_int(d, "ms", "calib.edge"),
        level=bool(_req(d, "lvl", (int, bool), "calib.edge")),
        dt_ms=_int(d, "dt", "calib.edge"),
    )


def _decode_calib_window(d: Mapping[str, Any]) -> CalibWindow:
    duty = _req(d, "duty", (int, float), "calib.window")
    return CalibWindow(
        ms=_int(d, "ms", "calib.window"),
        elapsed_ms=_int(d, "elapsed_ms", "calib.window"),
        edges=_int(d, "edges", "calib.window"),
        high_ms=_int(d, "high_ms", "calib.window"),
        duty_pct=float(duty),
        min_gap_ms=_int(d, "min_gap", "calib.window"),
        max_gap_ms=_int(d, "max_gap", "calib.window"),
        bounces=_int(d, "bounces", "calib.window"),
    )


def _decode_calib_done(d: Mapping[str, Any]) -> CalibDone:
    duty = _req(d, "duty", (int, float), "calib.done")
    return CalibDone(
        elapsed_ms=_int(d, "elapsed_ms", "calib.done"),
        edges=_int(d, "edges", "calib.done"),
        high_ms=_int(d, "high_ms", "calib.done"),
        duty_pct=float(duty),
        min_gap_ms=_int(d, "min_gap", "calib.done"),
        max_gap_ms=_int(d, "max_gap", "calib.done"),
        gaps=_int(d, "gaps", "calib.done"),
        bounces=_int(d, "bounces", "calib.done"),
    )


_DECODERS: dict[str, Callable[[Mapping[str, Any]], Message]] = {
    "hello": _decode_hello,
    "ready": _decode_ready,
    "hb": _decode_hb,
    "evt": _decode_evt,
    "ack": _decode_ack,
    "err": _decode_err,
    "calib.edge": _decode_calib_edge,
    "calib.window": _decode_calib_window,
    "calib.done": _decode_calib_done,
}

MAX_LINE = 512


def decode_line(line: str) -> Message:
    """Convierte una linea del enlace en un mensaje tipado."""
    text = line.strip()
    if not text:
        raise ProtocolError("linea vacia")
    if len(text) > MAX_LINE:
        raise ProtocolError(f"linea demasiado larga ({len(text)} > {MAX_LINE})")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"JSON invalido: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise ProtocolError("se esperaba un objeto JSON")

    kind = data.get("t")
    if not isinstance(kind, str):
        raise ProtocolError("falta el campo 't'")
    decoder = _DECODERS.get(kind)
    if decoder is None:
        raise ProtocolError(f"tipo de mensaje desconocido: '{kind}'")

    result = decoder(data)
    return result


# ---------------------------------------------------------------------------
# Codificacion (downlink)
# ---------------------------------------------------------------------------


def encode_cmd(cmd_id: int, op: str, **params: Any) -> str:
    """Construye una orden para el dispositivo."""
    if not op or " " in op:
        raise ValueError("op no puede estar vacio ni contener espacios")
    parts = ["cmd", str(cmd_id), op]
    for key, value in params.items():
        if " " in key or "=" in key:
            raise ValueError(f"clave de parametro invalida: {key!r}")
        parts.append(f"{key}={value}")
    return " ".join(parts)


def encode_ack_events(seq: int) -> str:
    """Confirma al dispositivo todos los eventos hasta ``seq``."""
    return f"ackevt {int(seq)}"
