"""Pruebas del codec del protocolo v1."""

from __future__ import annotations

import json

import pytest

from alarm.protocol.codec import (
    MAX_LINE,
    decode_line,
    encode_ack_events,
    encode_cmd,
)
from alarm.protocol.messages import (
    CalibDone,
    CalibEdge,
    CalibWindow,
    CommandAck,
    DeviceError,
    Heartbeat,
    Hello,
    MotionEvent,
    MotionKind,
    ProtocolError,
    Ready,
)


def _line(**payload) -> str:
    return json.dumps(payload)


# -- Decodificacion ---------------------------------------------------------


def test_hello_completo() -> None:
    raw = _line(
        t="hello",
        proto=1,
        dev="mega-pir",
        fw="1.1.0",
        boot=1234,
        zones=[{"id": 1, "pin": 2, "name": "pir"}],
        caps=["calibrate"],
        cfg={"warmup_ms": 60000},
    )
    msg = decode_line(raw)
    assert isinstance(msg, Hello)
    assert msg.dev == "mega-pir"
    assert msg.zones[0].pin == 2
    assert msg.cfg["warmup_ms"] == 60000


def test_hello_sin_cfg_usa_valores_por_defecto() -> None:
    raw = _line(t="hello", proto=1, dev="d", fw="1", boot=0, zones=[])
    msg = decode_line(raw)
    assert isinstance(msg, Hello)
    assert msg.cfg == {}
    assert msg.caps == ()


def test_ready_y_heartbeat() -> None:
    ready = decode_line(_line(t="ready", warmup_ms=60000))
    assert isinstance(ready, Ready)
    assert ready.warmup_ms == 60000

    hb = decode_line(_line(t="hb", up=62000, lvl=1, raw=14, buf=2, lost=0, calib=0, warmup=1))
    assert isinstance(hb, Heartbeat)
    assert hb.level is True
    assert hb.buffered == 2
    assert hb.warming_up is True
    assert hb.calibrating is False


def test_motion_eventos() -> None:
    start = decode_line(_line(t="evt", seq=7, kind="motion.start", z=1, ms=64495))
    assert isinstance(start, MotionEvent)
    assert start.kind is MotionKind.START
    assert start.dur_ms is None

    end = decode_line(_line(t="evt", seq=8, kind="motion.end", z=1, ms=67195, dur=2700))
    assert isinstance(end, MotionEvent)
    assert end.kind is MotionKind.END
    assert end.dur_ms == 2700


def test_calib_mensajes() -> None:
    edge = decode_line(_line(t="calib.edge", ms=1000, lvl=1, dt=2700))
    assert isinstance(edge, CalibEdge)
    assert edge.level is True

    window = decode_line(
        _line(
            t="calib.window",
            ms=5000,
            elapsed_ms=5000,
            edges=2,
            high_ms=5400,
            duty=27.4,
            min_gap=9000,
            max_gap=12000,
            bounces=0,
        )
    )
    assert isinstance(window, CalibWindow)
    assert window.duty_pct == pytest.approx(27.4)

    done = decode_line(
        _line(
            t="calib.done",
            elapsed_ms=60000,
            edges=3,
            high_ms=8100,
            duty=13.5,
            min_gap=9000,
            max_gap=22000,
            gaps=2,
            bounces=1,
        )
    )
    assert isinstance(done, CalibDone)
    assert done.gaps == 2


def test_ack_y_error() -> None:
    ack = decode_line(_line(t="ack", id=4, ok=True, op="calib.start"))
    assert isinstance(ack, CommandAck)
    assert ack.ok is True

    err = decode_line(_line(t="err", code="badop", msg="unknown op"))
    assert isinstance(err, DeviceError)
    assert err.code == "badop"


# -- Robustez ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "no es json",
        "[1, 2, 3]",
        '{"sin": "tipo"}',
        '{"t": 42}',
        '{"t": "desconocido"}',
        '{"t": "hb"}',
        '{"t": "hb", "up": "x", "raw": 1, "buf": 0, "lost": 0}',
        '{"t": "evt", "seq": 1, "kind": "inventado", "z": 1, "ms": 0}',
        '{"t": "hello", "proto": 1, "dev": "d", "fw": "1", "boot": 0, "zones": "x"}',
    ],
)
def test_lineas_invalidas_se_rechazan(raw: str) -> None:
    with pytest.raises(ProtocolError):
        decode_line(raw)


def test_linea_demasiado_larga() -> None:
    with pytest.raises(ProtocolError):
        decode_line('{"t": "hb", "pad": "' + "x" * MAX_LINE + '"}')


def test_bool_no_pasa_por_entero_donde_no_toca() -> None:
    with pytest.raises(ProtocolError):
        decode_line(_line(t="hb", up=True, raw=1, buf=0, lost=0))


def test_niveles_aceptan_cero_y_uno() -> None:
    hb = decode_line(_line(t="hb", up=1, lvl=0, raw=0, buf=0, lost=0))
    assert isinstance(hb, Heartbeat)
    assert hb.level is False


# -- Codificacion -----------------------------------------------------------


def test_encode_cmd_simple() -> None:
    assert encode_cmd(3, "ping") == "cmd 3 ping"


def test_encode_cmd_con_parametros() -> None:
    text = encode_cmd(7, "calib.start", window_ms=5000, auto_end_ms=60000)
    assert text == "cmd 7 calib.start window_ms=5000 auto_end_ms=60000"


def test_encode_cmd_rechaza_operaciones_invalidas() -> None:
    with pytest.raises(ValueError):
        encode_cmd(1, "")
    with pytest.raises(ValueError):
        encode_cmd(1, "dos palabras")
    with pytest.raises(ValueError):
        encode_cmd(1, "ping", **{"clave mala": 1})


def test_encode_ack_events() -> None:
    assert encode_ack_events(12) == "ackevt 12"


def test_roundtrip_del_downlink() -> None:
    """El downlink que genera el PC debe ser parseable por el dispositivo."""
    text = encode_cmd(9, "set", debounce_ms=50, hb_ms=2000)
    parts = text.split()
    assert parts[:3] == ["cmd", "9", "set"]
    assert dict(p.split("=") for p in parts[3:]) == {"debounce_ms": "50", "hb_ms": "2000"}


# -- Lineas reales capturadas del Mega 2560 --------------------------------
# Regresion: si el firmware cambia el formato, estas pruebas lo detectan.

REAL_LINES = [
    '{"t":"hello","proto":1,"dev":"mega-pir","fw":"1.1.0","boot":0,'
    '"zones":[{"id":1,"pin":2,"name":"pir"}],"caps":["hello","set","replay","calibrate"],'
    '"cfg":{"warmup_ms":60000,"debounce_ms":50,"hb_ms":2000,"calib_window_ms":5000}}',
    '{"t":"hb","up":4999,"lvl":0,"raw":1,"buf":0,"lost":0,"calib":0,"warmup":1}',
    '{"t":"ready","warmup_ms":60000}',
    '{"t":"hb","up":62000,"lvl":0,"raw":14,"buf":0,"lost":0,"calib":0}',
    '{"t":"evt","seq":2,"kind":"motion.start","z":1,"ms":64495}',
    '{"t":"evt","seq":3,"kind":"motion.end","z":1,"ms":67195,"dur":2700}',
    '{"t":"ack","id":1,"ok":true,"op":"hello"}',
    '{"t":"calib.window","ms":5000,"elapsed_ms":5000,"edges":2,"high_ms":5400,'
    '"duty":27.4,"min_gap":9000,"max_gap":12000,"bounces":0}',
    '{"t":"calib.done","elapsed_ms":60000,"edges":3,"high_ms":8100,"duty":13.5,'
    '"min_gap":9000,"max_gap":22000,"gaps":2,"bounces":0}',
]


@pytest.mark.parametrize("raw", REAL_LINES)
def test_lineas_reales_del_dispositivo(raw: str) -> None:
    assert decode_line(raw) is not None
