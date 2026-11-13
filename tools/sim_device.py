#!/usr/bin/env python3
"""Dispositivo simulado fuera de proceso.

Escribe las lineas del protocolo por stdout y acepta comandos por stdin, igual
que el Arduino. Util para probar el enlace, para depurar el parser y para
desarrollar sin hardware:

    python tools/sim_device.py --speed 6
    python tools/sim_device.py --speed 6 | python tools/alarmctl.py monitor

Sin --speed el tiempo avanza en tiempo real.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

from alarm.transport.simulated_link import SimulatedLink  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Arduino Mega simulado")
    parser.add_argument("--warmup", type=float, default=60.0, help="segundos de calentamiento")
    parser.add_argument("--hold-ms", type=int, default=2700, help="retencion del pulso")
    parser.add_argument("--motion-min", type=float, default=8.0)
    parser.add_argument("--motion-max", type=float, default=20.0)
    parser.add_argument("--noise", type=float, default=0.0, help="falsos flancos por minuto")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    link = SimulatedLink(
        warmup_s=args.warmup,
        hold_ms=args.hold_ms,
        motion_every_s=(args.motion_min, args.motion_max),
        false_edges_per_min=args.noise,
        speed=args.speed,
        seed=args.seed,
    )

    stop = threading.Event()

    def pump_stdin() -> None:
        for raw in sys.stdin:
            text = raw.strip()
            if not text:
                continue
            try:
                link.write_line(text)
            except Exception:  # noqa: BLE001
                break
            if text == "quit":
                break
        stop.set()

    threading.Thread(target=pump_stdin, daemon=True).start()
    link.open()

    try:
        while not stop.is_set():
            out = link.read_line(0.1)
            if out is not None:
                print(out, flush=True)
            else:
                time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
