"""Enlace simulado: un Arduino falso en proceso.

Permite desarrollar y probar todo el software sin hardware conectado. Genera
exactamente las mismas lineas que el firmware real, incluido el modo de
calibracion, y responde a los comandos del downlink.

Con ``speed > 1`` el tiempo simulado avanza mas rapido que el real: una
calibracion de 60 s se prueba en 6 s.
"""

from __future__ import annotations

import json
import random
import time
from typing import Any

from .base import LinkError

FW_VERSION = "1.2.0-sim"

BEEP_NAMES = ("armed", "disarmed", "detect", "alarm", "error", "test")


class SimulatedLink:
    def __init__(
        self,
        *,
        warmup_s: float = 60.0,
        hb_ms: int = 2000,
        motion_every_s: tuple[float, float] = (8.0, 20.0),
        hold_ms: int = 2700,
        false_edges_per_min: float = 0.0,
        speed: float = 1.0,
        seed: int | None = None,
    ) -> None:
        self._warmup_s = warmup_s
        self._hb_ms = hb_ms
        self._motion_range = motion_every_s
        self._hold_ms = hold_ms
        self._false_rate = false_edges_per_min
        self._speed = max(0.01, speed)
        self._rng = random.Random(seed)

        self._open = False
        self._t0 = 0.0
        self._queue: list[str] = []

        self._seq = 0
        self._level = False
        self._raw = 0
        self._lost = 0
        self._armed = False
        self._buzzer_on = True
        self._siren_ms = 120_000
        self.last_beep: str | None = None

        self._next_hb = 0.0
        self._next_motion = 0.0
        self._motion_end: float | None = None
        self._next_false = 0.0
        self._warmed = False
        self._tick_s = 0.0

        self._calib = False
        self._calib_started = 0.0
        self._calib_window_s = 5.0
        self._calib_auto_end_s = 60.0
        self._win_start = 0.0
        self._win_edges = 0
        self._win_high_ms = 0
        self._win_min_gap = 0
        self._win_max_gap = 0
        self._win_bounces = 0
        self._last_rise: int | None = None

        self._tot_edges = 0
        self._tot_high_ms = 0
        self._tot_min_gap = 0
        self._tot_max_gap = 0
        self._tot_gaps = 0
        self._tot_bounces = 0

    # -- API de Link -------------------------------------------------------

    @property
    def name(self) -> str:
        return "<simulado>"

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True
        self._t0 = time.monotonic()
        self._next_hb = 0.0
        self._queue.append(self._hello_json())

    def close(self) -> None:
        self._open = False
        self._queue.clear()

    def read_line(self, timeout: float) -> str | None:
        if not self._open:
            raise LinkError("enlace simulado cerrado")

        deadline = time.monotonic() + timeout
        while True:
            self._advance()
            if self._queue:
                return self._queue.pop(0)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.005)

    def write_line(self, text: str) -> None:
        if not self._open:
            raise LinkError("enlace simulado cerrado")
        self._handle(text.strip())

    # -- Reloj simulado ----------------------------------------------------

    def _now_s(self) -> float:
        return (time.monotonic() - self._t0) * self._speed

    def _now_ms(self) -> int:
        return int(self._now_s() * 1000)

    def _advance(self) -> None:
        now = self._now_s()

        if not self._warmed and now >= self._warmup_s:
            self._warmed = True
            self._push({"t": "ready", "warmup_ms": int(self._warmup_s * 1000)})
            self._next_motion = now + self._rng.uniform(*self._motion_range)
            if self._false_rate > 0:
                self._next_false = now + 60.0 / self._false_rate

        while self._now_s() >= self._next_hb:
            self._next_hb += self._hb_ms / 1000.0
            payload: dict[str, Any] = {
                "t": "hb",
                "up": self._now_ms(),
                "lvl": 1 if self._level else 0,
                "raw": self._raw,
                "buf": 0,
                "lost": self._lost,
                "calib": 1 if self._calib else 0,
            }
            if not self._warmed:
                payload["warmup"] = 1
            self._push(payload)

        if not self._warmed:
            self._tick_s = now
            return

        # Tiempo alta acumulado por tic, igual que el firmware: asi una ventana
        # nunca contabiliza un pulso entero si este empezo antes.
        if self._calib and self._level:
            delta_ms = int((now - self._tick_s) * 1000)
            if delta_ms > 0:
                self._win_high_ms += delta_ms
                self._tot_high_ms += delta_ms
        self._tick_s = now

        if self._motion_end is not None and now >= self._motion_end:
            self._motion_end = None
            self._edge(False, now, self._hold_ms)

        if self._motion_end is None and now >= self._next_motion:
            self._next_motion = now + self._rng.uniform(*self._motion_range)
            self._motion_end = now + self._hold_ms / 1000.0
            self._edge(True, now, 0)

        if self._false_rate > 0 and now >= self._next_false:
            self._next_false = now + 60.0 / self._false_rate
            self._raw += 1
            self._lost += 1
            if self._calib:
                self._win_bounces += 1
                self._tot_bounces += 1

        if self._calib:
            if now - self._win_start >= self._calib_window_s:
                self._flush_window(now - self._win_start)
            if now - self._calib_started >= self._calib_auto_end_s:
                self._stop_calib()

    # -- Emision -----------------------------------------------------------

    def _push(self, payload: dict[str, Any]) -> None:
        self._queue.append(json.dumps(payload))

    def _hello_json(self) -> str:
        return json.dumps(
            {
                "t": "hello",
                "proto": 1,
                "dev": "mega-pir",
                "fw": FW_VERSION,
                "boot": 1234,
                "zones": [{"id": 1, "pin": 2, "name": "pir"}],
                "caps": ["hello", "set", "replay", "calibrate", "beep"],
                "cfg": {
                    "warmup_ms": int(self._warmup_s * 1000),
                    "debounce_ms": 50,
                    "hb_ms": self._hb_ms,
                    "calib_window_ms": int(self._calib_window_s * 1000),
                    "buzzer_pin": 11,
                    "buzzer": 1 if self._buzzer_on else 0,
                    "armed": 1 if self._armed else 0,
                },
            }
        )

    def _edge(self, rising: bool, now: float, dt_ms: int) -> None:
        self._level = rising
        self._raw += 1
        ms = int(now * 1000)
        self._seq += 1

        if rising:
            self._push({"t": "evt", "seq": self._seq, "kind": "motion.start", "z": 1, "ms": ms})
            # El firmware real pita aqui si esta armado; el simulador lo imita.
            if self._armed and self._buzzer_on:
                self.last_beep = "detect"
            if self._calib:
                self._push({"t": "calib.edge", "ms": ms, "lvl": 1, "dt": dt_ms})
                self._win_edges += 1
                self._tot_edges += 1
                if self._last_rise is not None:
                    gap = ms - self._last_rise
                    self._win_min_gap = (
                        gap if not self._win_min_gap else min(self._win_min_gap, gap)
                    )
                    self._win_max_gap = max(self._win_max_gap, gap)
                    self._tot_min_gap = (
                        gap if not self._tot_min_gap else min(self._tot_min_gap, gap)
                    )
                    self._tot_max_gap = max(self._tot_max_gap, gap)
                    self._tot_gaps += 1
                self._last_rise = ms
        else:
            self._push(
                {
                    "t": "evt",
                    "seq": self._seq,
                    "kind": "motion.end",
                    "z": 1,
                    "ms": ms,
                    "dur": self._hold_ms,
                }
            )
            if self._calib:
                self._push({"t": "calib.edge", "ms": ms, "lvl": 0, "dt": dt_ms})

    @staticmethod
    def _pct(part_ms: int, total_ms: int) -> float:
        if total_ms <= 0:
            return 0.0
        return round(min(100.0, part_ms * 100.0 / total_ms), 1)

    def _flush_window(self, elapsed_s: float) -> None:
        elapsed_ms = max(1, int(elapsed_s * 1000))
        self._push(
            {
                "t": "calib.window",
                "ms": self._now_ms(),
                "elapsed_ms": elapsed_ms,
                "edges": self._win_edges,
                "high_ms": self._win_high_ms,
                "duty": self._pct(self._win_high_ms, elapsed_ms),
                "min_gap": self._win_min_gap,
                "max_gap": self._win_max_gap,
                "bounces": self._win_bounces,
            }
        )
        self._win_start = self._now_s()
        self._win_edges = 0
        self._win_high_ms = 0
        self._win_min_gap = 0
        self._win_max_gap = 0
        self._win_bounces = 0

    def _start_calib(self, window_ms: int, auto_end_ms: int) -> None:
        self._calib = True
        self._calib_started = self._now_s()
        self._calib_window_s = max(0.5, window_ms / 1000.0)
        self._calib_auto_end_s = max(1.0, auto_end_ms / 1000.0)
        self._win_start = self._now_s()
        self._win_edges = 0
        self._win_high_ms = 0
        self._win_min_gap = 0
        self._win_max_gap = 0
        self._win_bounces = 0
        self._last_rise = None
        self._tot_edges = 0
        self._tot_high_ms = 0
        self._tot_min_gap = 0
        self._tot_max_gap = 0
        self._tot_gaps = 0
        self._tot_bounces = 0

    def _stop_calib(self) -> None:
        if not self._calib:
            return
        now = self._now_s()
        # Cerrar solo la ventana en curso, no toda la sesion.
        if now - self._win_start >= 0.5:
            self._flush_window(now - self._win_start)
        total_ms = max(1, int((now - self._calib_started) * 1000))
        self._push(
            {
                "t": "calib.done",
                "elapsed_ms": total_ms,
                "edges": self._tot_edges,
                "high_ms": self._tot_high_ms,
                "duty": self._pct(self._tot_high_ms, total_ms),
                "min_gap": self._tot_min_gap,
                "max_gap": self._tot_max_gap,
                "gaps": self._tot_gaps,
                "bounces": self._tot_bounces,
            }
        )
        self._calib = False

    # -- Downlink ----------------------------------------------------------

    def _handle(self, line: str) -> None:
        parts = line.split()
        if not parts:
            return
        if parts[0] == "ackevt":
            return
        if parts[0] != "cmd" or len(parts) < 3:
            self._push({"t": "err", "code": "badcmd", "msg": "unknown verb"})
            return

        try:
            cmd_id = int(parts[1])
        except ValueError:
            self._push({"t": "err", "code": "badcmd", "msg": "id no numerico"})
            return

        op = parts[2]
        params: dict[str, str] = {}
        for token in parts[3:]:
            key, _, value = token.partition("=")
            params[key] = value

        ok = True
        if op == "hello":
            self._queue.append(self._hello_json())
        elif op == "ping":
            pass
        elif op == "set":
            if "hb_ms" in params:
                self._hb_ms = max(500, int(params["hb_ms"]))
            if "warmup_ms" in params:
                self._warmup_s = max(0, int(params["warmup_ms"])) / 1000.0
            if "armed" in params:
                self._armed = int(params["armed"]) != 0
            if "buzzer" in params:
                self._buzzer_on = int(params["buzzer"]) != 0
            if "siren_ms" in params:
                self._siren_ms = max(1000, int(params["siren_ms"]))
        elif op == "beep":
            name = params.get("name", "test")
            if name not in BEEP_NAMES:
                ok = False
                self._push({"t": "err", "code": "badop", "msg": "unknown beep"})
            elif not self._buzzer_on:
                # El dispositivo real tambien acusa sin sonar.
                self.last_beep = None
            else:
                self.last_beep = name
        elif op == "replay":
            pass
        elif op == "reset":
            self._raw = 0
            self._lost = 0
        elif op == "calib.start":
            self._start_calib(
                int(params.get("window_ms", 5000)),
                int(params.get("auto_end_ms", 60000)),
            )
        elif op == "calib.stop":
            self._stop_calib()
        else:
            ok = False
            self._push({"t": "err", "code": "badop", "msg": "unknown op"})

        self._push({"t": "ack", "id": cmd_id, "ok": ok, "op": op})
