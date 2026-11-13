#!/usr/bin/env python3
"""alarmctl - utilidades de linea de comandos de la alarma.

Subcomandos:
  info         identifica el dispositivo y su configuracion
  monitor      muestra eventos y latidos en vivo
  calibrate    asistente de ajuste de los potenciometros del PIR
  beep         prueba el zumbador con un patron concreto
  notify-test  envia un correo de prueba con la configuracion real
  ntfy-test    envia una notificacion de prueba al movil (ntfy)
  remote-send  publica una orden de mando remoto, como lo haria el movil
  remote-listen escucha el tema de mando y dice si la clave o el TOTP valen
  log          consulta y exporta el historial guardado, sin abrir la ventana
  backup       guarda o restaura la configuracion completa, credenciales incluidas
  ports        lista los puertos serie candidatos

Uso:
  python tools/alarmctl.py info
  python tools/alarmctl.py monitor --seconds 30
  python tools/alarmctl.py calibrate --quiet-s 30 --motion-s 45
  python tools/alarmctl.py beep --name alarm --ms 3000
  python tools/alarmctl.py beep --name tone --freq 2700 --ms 1200
  python tools/alarmctl.py notify-test
  python tools/alarmctl.py ntfy-test
  python tools/alarmctl.py remote-send --action armar
  python tools/alarmctl.py remote-send --action consultar --key 123456
  python tools/alarmctl.py remote-listen --seconds 60
  python tools/alarmctl.py remote-listen --all --seconds 30
  python tools/alarmctl.py log --limit 50
  python tools/alarmctl.py log --since -24h --tag triggered
  python tools/alarmctl.py log --since 2026-11-01 --csv registro.csv
  python tools/alarmctl.py backup export --out copia.alarmbackup
  python tools/alarmctl.py backup import --from copia.alarmbackup
  python tools/alarmctl.py --sim calibrate --no-wait          (sin hardware)
"""

from __future__ import annotations

import argparse
import contextlib
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bootstrap import bootstrap  # noqa: E402

bootstrap()

from alarm.device.calibrator import (  # noqa: E402
    Calibrator,
    Finding,
    Level,
    SessionStats,
    evaluate,
    recommendation,
)
from alarm.device.manager import DeviceManager  # noqa: E402
from alarm.protocol.messages import (  # noqa: E402
    CalibWindow,
    DeviceError,
    Heartbeat,
    Hello,
    Message,
    MotionEvent,
    MotionKind,
    Ready,
)
from alarm.transport.serial_link import SerialLink, describe_ports, list_candidates  # noqa: E402
from alarm.transport.simulated_link import SimulatedLink  # noqa: E402
from alarm.ui.audio import beep  # noqa: E402
from alarm.ui.audio import set_enabled as set_beeps_enabled  # noqa: E402

# Codigos de salida. Un veredicto sobre el sensor no es un error del programa,
# y quien encadene esto en un script debe poder distinguirlos.
EXIT_SENSOR_OK = 0
EXIT_SENSOR_FAIL = 1
EXIT_CONNECTION = 2

# ---------------------------------------------------------------------------
# Presentacion
# ---------------------------------------------------------------------------

_COLOR = sys.stdout.isatty()
_ANSI = {
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[36m",
    "reset": "\033[0m",
}


def paint(text: str, style: str) -> str:
    if not _COLOR or style not in _ANSI:
        return text
    return f"{_ANSI[style]}{text}{_ANSI['reset']}"


def head(text: str) -> None:
    print()
    print(paint(text, "bold"))
    print(paint("-" * len(text), "dim"))


def line(text: str = "") -> None:
    print(text)


def ok(text: str) -> None:
    print(f"  {paint('OK  ', 'green')} {text}")


def warn(text: str) -> None:
    print(f"  {paint('AVISO', 'yellow')} {text}")


def fail(text: str) -> None:
    print(f"  {paint('FALLO', 'red')} {text}")


def info(text: str) -> None:
    print(f"  {paint('info', 'blue')} {text}")


def bar(pct: float, width: int = 28) -> str:
    filled = int(round(max(0.0, min(100.0, pct)) / 100.0 * width))
    return "[" + "#" * filled + "." * (width - filled) + "]"


LEVEL_MARK = {
    Level.OK: ("OK   ", "green"),
    Level.WARN: ("AVISO", "yellow"),
    Level.FAIL: ("FALLO", "red"),
    Level.INFO: ("info ", "blue"),
}


def print_finding(f: Finding) -> None:
    mark, style = LEVEL_MARK[f.level]
    print(f"  {paint(mark, style)} {paint(f.title, 'bold')}")
    for chunk in _wrap(f.detail, 74):
        print(f"          {chunk}")


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        if not current:
            current = w
        elif len(current) + 1 + len(w) <= width:
            current += " " + w
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def fmt_ms(ms: int) -> str:
    if ms >= 60000:
        return f"{ms / 60000:.1f} min"
    if ms >= 1000:
        return f"{ms / 1000:.1f} s"
    return f"{ms} ms"


# ---------------------------------------------------------------------------
# Construccion del enlace
# ---------------------------------------------------------------------------


def build_link(args: argparse.Namespace):
    if args.sim:
        return SimulatedLink(
            warmup_s=args.sim_warmup,
            speed=args.sim_speed,
            motion_every_s=(4.0, 9.0),
            hold_ms=2700,
            seed=args.sim_seed,
        )
    return SerialLink(port=args.port, baudrate=args.baud)


def connect(link, *, quiet: bool = False) -> DeviceManager:
    manager = DeviceManager(link, auto_ack=True)
    if not quiet:
        manager.on_health = lambda h: None
    manager.start()
    return manager


# ---------------------------------------------------------------------------
# Subcomando: ports
# ---------------------------------------------------------------------------


def cmd_ports(_args: argparse.Namespace) -> int:
    head("Puertos serie detectados")
    entries = describe_ports()
    if not entries:
        warn("ninguno")
        return 1
    for e in entries:
        line(f"  {e}")
    candidates = list_candidates()
    line()
    if candidates:
        ok(f"candidatos Arduino: {', '.join(candidates)}")
    else:
        warn("ningun candidato reconocido por VID/PID")
    return 0


# ---------------------------------------------------------------------------
# Subcomando: info
# ---------------------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    link = build_link(args)
    manager = connect(link)
    try:
        head("Conectando")
        if not manager.wait_connected(args.timeout):
            fail(f"sin respuesta del dispositivo en {args.timeout:.0f} s")
            if manager.stats.last_error:
                info(f"ultimo error: {manager.stats.last_error}")
            return EXIT_CONNECTION

        hello: Hello = manager.hello  # type: ignore[assignment]
        line(f"  dispositivo : {hello.dev}  (protocolo v{hello.proto})")
        line(f"  firmware    : {hello.fw}")
        line(f"  arranque    : {hello.boot_ms} ms")
        line(f"  capacidades : {', '.join(hello.caps) or '-'}")
        for z in hello.zones:
            line(f"  zona {z.id}      : {z.name} en D{z.pin}")
        if hello.cfg:
            line()
            for key, value in sorted(hello.cfg.items()):
                line(f"  {key:<16}: {value}")
        line()
        info(f"enlace: {link.name}")
        return 0
    finally:
        manager.stop()


# ---------------------------------------------------------------------------
# Subcomando: monitor
# ---------------------------------------------------------------------------


def cmd_monitor(args: argparse.Namespace) -> int:
    link = build_link(args)
    start = time.monotonic()
    ready = threading.Event()
    ready_seen = False
    hello_seen = False
    hb_count = 0

    def on_message(msg: Message) -> None:
        nonlocal ready_seen, hb_count, hello_seen
        elapsed = time.monotonic() - start

        if isinstance(msg, Hello):
            if not hello_seen:
                hello_seen = True
                line(f"  {paint('conectado', 'blue')}  {msg.dev} fw {msg.fw}")
        elif isinstance(msg, Ready):
            ready_seen = True
            ready.set()
            ok(f"PIR listo tras {fmt_ms(msg.warmup_ms)} de calentamiento")
        elif isinstance(msg, Heartbeat):
            if not ready_seen and not msg.warming_up:
                ready_seen = True
                ready.set()
            hb_count += 1
            if args.show_hb:
                line(
                    f"{elapsed:7.1f}s  {paint('hb ', 'dim')} "
                    f"up={fmt_ms(msg.up_ms)} nivel={int(msg.level)} "
                    f"flanco={msg.raw_edges} buffer={msg.buffered} perdidos={msg.lost}"
                )
        elif isinstance(msg, MotionEvent):
            style = "green" if msg.kind is MotionKind.START else "blue"
            extra = ""
            if msg.kind is MotionKind.END and msg.dur_ms is not None:
                extra = f"  dur={fmt_ms(msg.dur_ms)}"
            line(
                f"{elapsed:7.1f}s  {paint(msg.kind.value, style)} "
                f"seq={msg.seq} z={msg.zone} t={fmt_ms(msg.ms)}{extra}"
            )
        elif isinstance(msg, DeviceError):
            warn(f"dispositivo: {msg.code} {msg.msg}")

    manager = DeviceManager(link, auto_ack=True, on_message=on_message)
    manager.start()
    try:
        if not manager.wait_connected(args.timeout):
            fail("sin respuesta del dispositivo")
            return EXIT_CONNECTION

        if args.skip_warmup:
            manager.wait_ack(manager.send("set", warmup_ms=0), 2.0)

        head(f"Monitorizando {link.name} ({args.seconds:.0f} s)")
        if not ready_seen:
            info("esperando a que el PIR termine el calentamiento...")
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            time.sleep(0.1)
    except KeyboardInterrupt:
        line()
        info("interrumpido")
    finally:
        manager.stop()

    head("Resumen")
    line(f"  eventos   : {manager.stats.events_rx}")
    line(f"  lineas    : {manager.stats.lines_rx}")
    line(f"  latidos   : {hb_count}")
    if manager.stats.decode_errors:
        warn(f"lineas descartadas: {manager.stats.decode_errors}")
    return 0


# ---------------------------------------------------------------------------
# Subcomando: calibrate
# ---------------------------------------------------------------------------


class _PhaseRunner:
    """Ejecuta una fase de calibracion y muestra el progreso."""

    def __init__(self, manager: DeviceManager, phase: str) -> None:
        self.manager = manager
        self.calibrator = Calibrator(phase)
        self.done = threading.Event()
        self.windows = 0
        self._high_ms = 0
        self._elapsed_ms = 0
        self._total_s = 0.0
        manager.on_message = self._on_message

    @property
    def duty_pct(self) -> float:
        if self._elapsed_ms <= 0:
            return 0.0
        return min(100.0, self._high_ms * 100.0 / self._elapsed_ms)

    def _on_message(self, msg: Message) -> None:
        if isinstance(msg, CalibWindow):
            self.windows += 1
            self.calibrator.feed(msg)
            self._high_ms += msg.high_ms
            self._elapsed_ms += msg.elapsed_ms
            gap_min = fmt_ms(msg.min_gap_ms) if msg.min_gap_ms else "-"
            gap_max = fmt_ms(msg.max_gap_ms) if msg.max_gap_ms else "-"
            restante = max(0.0, self._total_s - self._elapsed_ms / 1000.0)
            print(
                f"    {paint(f'{self.windows:2d}', 'dim')}  "
                f"{bar(self.duty_pct)} {self.duty_pct:5.1f}%  "
                f"flancos={msg.edges}  gap={gap_min}/{gap_max}  "
                f"quedan {restante:4.0f}s"
                f"{'  rebotes=' + str(msg.bounces) if msg.bounces else ''}",
                flush=True,
            )
        else:
            self.calibrator.feed(msg)
            if self.calibrator.finished:
                self.done.set()

    def run(self, seconds: float, window_ms: int) -> SessionStats:
        self._total_s = seconds
        ack = self.manager.wait_ack(
            self.manager.send("calib.start", window_ms=window_ms, auto_end_ms=int(seconds * 1000)),
            3.0,
        )
        if ack is None or not ack.ok:
            raise RuntimeError("el dispositivo rechazo calib.start")
        if not self.done.wait(seconds + 20):
            warn("el dispositivo no cerro la fase; se corta a mano")
            self.manager.send("calib.stop")
            self.done.wait(5)
        return self.calibrator.stats()


def _wait_ready(manager: DeviceManager, timeout: float) -> bool:
    if manager.hello and "warmup_ms" in manager.hello.cfg:
        pass
    ready = threading.Event()
    previous = manager.on_message

    def probe(msg: Message) -> None:
        if isinstance(msg, Ready) or isinstance(msg, Heartbeat) and not msg.warming_up:
            ready.set()
        if previous is not None:
            previous(msg)

    manager.on_message = probe
    return ready.wait(timeout)


def cmd_calibrate(args: argparse.Namespace) -> int:
    link = build_link(args)
    manager = connect(link)
    try:
        head("Modo ajuste del sensor")
        if not manager.wait_connected(args.timeout):
            fail("sin respuesta del dispositivo")
            return EXIT_CONNECTION

        hello: Hello = manager.hello  # type: ignore[assignment]
        line(f"  dispositivo: {hello.dev} fw {hello.fw} en {link.name}")

        warmup_s = hello.cfg.get("warmup_ms", 60000) / 1000.0
        if args.skip_warmup:
            manager.wait_ack(manager.send("set", warmup_ms=0), 2.0)
            info("calentamiento omitido a proposito (--skip-warmup)")
        else:
            info(f"esperando el calentamiento del PIR ({warmup_s:.0f} s)...")
            if not _wait_ready(manager, warmup_s + 15):
                warn("no llego el aviso de 'ready'; se continua igualmente")

        print()
        line(paint("Los potenciometros son fisicos: este asistente mide su efecto.", "dim"))
        line(paint("  Sx = sensibilidad y alcance    Tx = tiempo que la salida sigue alta", "dim"))

        # -- Preparacion -----------------------------------------------------
        # La fase de reposo solo vale si nadie esta delante del sensor. Quien
        # lanza el asistente suele estar sentado justo enfrente, asi que hace
        # falta un margen explicito para que salga de la estancia.
        if args.prep_s > 0:
            head(f"Preparacion ({args.prep_s:.0f} s)")
            line("  Sal de la estancia AHORA. La medicion empieza al acabar la cuenta.")
            beep("leave")
            for remaining in range(int(args.prep_s), 0, -1):
                print(f"\r  empieza en {remaining:2d} s  ", end="", flush=True)
                time.sleep(1)
            print("\r" + " " * 24 + "\r", end="", flush=True)
            line("  midiendo...")

        # -- Fase 1: reposo --------------------------------------------------
        head(f"Fase 1/2 - Reposo ({args.quiet_s:.0f} s)")
        line("  No te muevas delante del sensor. Mide falsos positivos.")
        if not args.no_wait:
            input(paint("  Pulsa ENTER cuando estes fuera del alcance... ", "bold"))
        runner = _PhaseRunner(manager, "quiet")
        line()
        quiet = runner.run(args.quiet_s, window_ms=2000)
        line()
        line(f"  resultado: {quiet.edges} detecciones, {quiet.bounces} rebotes")

        # -- Fase 2: movimiento ----------------------------------------------
        head(f"Fase 2/2 - Movimiento ({args.motion_s:.0f} s)")
        line("  Cruza la estancia de lado, 3 o 4 veces, con pausas de 3 s.")
        if not args.no_wait:
            input(paint("  Pulsa ENTER y empieza a moverte... ", "bold"))
        beep("move")
        runner = _PhaseRunner(manager, "motion")
        line()
        motion = runner.run(args.motion_s, window_ms=3000)
        beep("done")
        line()
        line(f"  resultado: {motion.pulses} pulsos, retencion media {fmt_ms(motion.hold_avg_ms)}")

        # -- Informe ---------------------------------------------------------
        findings = evaluate(quiet, motion)
        head("Diagnostico")
        for f in findings:
            print_finding(f)

        head("Conclusion")
        print(f"  {recommendation(findings)}")
        line()
        # Codigo de salida: 0 sensor correcto, 1 requiere atencion. Un 2 se
        # reserva para fallos de conexion. Sirve para encadenar en scripts,
        # pero conviene tenerlo documentado: un 1 aqui NO es un error del
        # programa, es un veredicto sobre el sensor.
        healthy = all(f.level is not Level.FAIL for f in findings)
        return EXIT_SENSOR_OK if healthy else EXIT_SENSOR_FAIL
    finally:
        manager.stop()


# ---------------------------------------------------------------------------
# Subcomando: beep
# ---------------------------------------------------------------------------


def cmd_beep(args: argparse.Namespace) -> int:
    """Prueba el zumbador del dispositivo sin abrir la ventana.

    La prueba se envia con `force=1`: es una comprobacion manual del cableado y
    debe sonar aunque el PIR siga en calentamiento, que es justo cuando uno
    conecta la placa y quiere saber si el piezo esta bien puesto.
    """
    link = build_link(args)
    manager = connect(link)
    try:
        head("Prueba del zumbador")
        if not manager.wait_connected(args.timeout):
            fail(f"sin respuesta del dispositivo en {args.timeout:.0f} s")
            return EXIT_CONNECTION
        if args.skip_warmup:
            manager.wait_ack(manager.send("set", warmup_ms=0), 2.0)
        elif manager.hello is not None:
            warmup_ms = manager.hello.cfg.get("warmup_ms", 0)
            if warmup_ms:
                info(
                    f"el PIR sigue en calentamiento ({warmup_ms / 1000:.0f} s); "
                    "la prueba suena igual porque se envia forzada."
                )

        params: dict[str, object] = {"name": args.name, "ms": args.ms, "force": 1}
        if args.name == "tone":
            params["freq"] = args.freq
        ack = manager.wait_ack(manager.send("beep", **params), 3.0)
        if ack is None:
            fail("sin respuesta al comando beep")
            return EXIT_CONNECTION
        if not ack.ok:
            fail(f"el dispositivo rechazo el patron '{args.name}'")
            if manager.hello is not None and not manager.hello.cfg.get("buzzer", 1):
                info("el zumbador esta apagado en la placa: envia `set buzzer=1`")
            return EXIT_SENSOR_FAIL
        if args.name == "tone":
            ok(f"tono de {args.freq} Hz durante {args.ms or 1000} ms enviado")
        else:
            ok(f"patron '{args.name}' enviado")
        return 0
    finally:
        manager.stop()


# ---------------------------------------------------------------------------
# Subcomando: notify-test
# ---------------------------------------------------------------------------


def cmd_notify_test(_args: argparse.Namespace) -> int:
    """Envia un correo de prueba con la configuracion real."""
    from alarm.config import ConfigError, load_config
    from alarm.services.email_sender import EmailChannel

    head("Prueba de correo")
    try:
        config = load_config()
    except ConfigError as exc:
        fail(str(exc))
        return 1

    email = config.email
    line(f"  servidor : {email.host}:{email.port} ({email.security})")
    line(f"  de       : {email.sender or '-'}")
    line(f"  para     : {', '.join(email.to) or '-'}")
    if not email.configured:
        line()
        fail(
            "faltan datos: host, remitente, destinatarios o credenciales. "
            "Revisa config/alarm.toml y .env"
        )
        return 1
    if not email.enabled:
        warn("el correo esta deshabilitado en la configuracion; se envia igualmente")

    channel = EmailChannel(email)
    channel.set_enabled(True)
    subject = email.subject.format(evento="Prueba", hora=time.strftime("%H:%M:%S"))
    line()
    info("enviando...")
    result = channel.send(
        subject,
        "Mensaje de prueba de la alarma de presencia.\n\nSi lo lees, el envio funciona.\n",
    )
    line()
    if result.ok:
        ok(result.detail)
        return 0
    fail(result.detail)
    return 1


# ---------------------------------------------------------------------------
# Subcomando: ntfy-test
# ---------------------------------------------------------------------------


def cmd_ntfy_test(args: argparse.Namespace) -> int:
    """Envia una notificacion de prueba con la configuracion real."""
    from alarm.config import ConfigError, load_config, load_env
    from alarm.services.ntfy_sender import NtfyChannel

    head("Prueba de aviso al movil (ntfy)")
    config_path, local_path, env_path = _config_paths(args)
    try:
        config = load_config(config_path, env=load_env(env_path), local=local_path)
    except ConfigError as exc:
        fail(str(exc))
        return 1

    ntfy = config.ntfy
    line(f"  servidor : {ntfy.base_url or '-'}")
    line(f"  tema     : {ntfy.topic or '-'}")
    line(f"  token    : {'configurado' if ntfy.token else 'no (servidor publico)'}")
    line(f"  prioridad: {ntfy.priority_triggered}")
    if not ntfy.configured:
        line()
        fail(
            f"faltan datos: {', '.join(ntfy.missing_fields)}. Revisa la seccion "
            "[notify.ntfy] de config/alarm.toml"
        )
        return 1
    if not ntfy.enabled:
        warn("el aviso al movil esta deshabilitado en la configuracion; se envia igualmente")
    if ntfy.uses_public_server and len(ntfy.topic) < 12:
        warn("en ntfy.sh un tema corto se puede adivinar; usa uno largo y aleatorio")

    channel = NtfyChannel(ntfy)
    channel.set_enabled(True)
    title = args.title or ntfy.title.format(evento="Prueba", hora=time.strftime("%H:%M:%S"))
    line()
    info("enviando...")
    result = channel.send(
        title,
        "Mensaje de prueba de la alarma de presencia.\n\n"
        "Si lo ves en el movil, el aviso por ntfy funciona.\n",
        cause="triggered",
    )
    line()
    if result.ok:
        ok(result.detail)
        return 0
    fail(result.detail)
    return 1


# ---------------------------------------------------------------------------
# Subcomandos: mando remoto
# ---------------------------------------------------------------------------

#: Palabra que se publica para cada accion del mando.
_REMOTE_WORDS: dict[str, str] = {
    "armar": "armar",
    "desarmar": "desarmar",
    "consultar": "consultar",
    "silenciar": "silenciar",
    "panic": "panico",
}


def cmd_remote_send(args: argparse.Namespace) -> int:
    """Publica una orden en el tema de mando, como lo haria el movil."""
    from alarm.config import ConfigError, load_config, load_env
    from alarm.services.ntfy_sender import publish
    from alarm.services.totp import code_at

    head("Mando remoto: enviar una orden")
    config_path, local_path, env_path = _config_paths(args)
    try:
        config = load_config(config_path, env=load_env(env_path), local=local_path)
    except ConfigError as exc:
        fail(str(exc))
        return 1

    topic = args.topic.strip() or config.remote_topic
    if not topic:
        fail(
            "no hay tema de mando: escribe [remote] topic o rellena [notify.ntfy] topic "
            "en la configuracion"
        )
        return 1
    if not config.ntfy.configured:
        line()
        fail(
            f"faltan datos del servidor: {', '.join(config.ntfy.missing_fields)}. "
            "Revisa [notify.ntfy] en config/alarm.toml"
        )
        return 1

    credential = args.key.strip()
    kind = "clave indicada a mano"
    if not credential:
        if config.remote.totp_secret:
            credential = code_at(config.remote.totp_secret)
            kind = "codigo TOTP de este instante"
        elif config.remote.key:
            credential = config.remote.key
            kind = "clave de la configuracion"
    if not credential:
        warn("no hay clave ni TOTP configurados: la orden se enviara sin credencial")
        kind = "ninguna (sera rechazada)"

    word = _REMOTE_WORDS[args.action]
    message = f"{credential} {word}".strip()
    line(f"  servidor  : {config.ntfy.base_url}")
    line(f"  tema      : {topic}")
    line(f"  orden     : {word}")
    line(f"  credencial: {kind}")
    if config.remote.uses_totp and args.key.strip() and args.key.strip().isdigit():
        warn("has pasado un codigo a mano: caduca en menos de 30 s")
    line()
    info("publicando...")
    result = publish(
        config.ntfy,
        topic=topic,
        title="Prueba de mando",
        message=message,
        priority="default",
        tags=("mando-send",),
    )
    line()
    if result.ok:
        ok(result.detail)
        info("si la app esta abierta, la respuesta llega al tema de avisos")
        return 0
    fail(result.detail)
    return 1


def cmd_remote_listen(args: argparse.Namespace) -> int:
    """Escucha el tema de mando y dice si cada mensaje es una orden valida.

    No ejecuta nada: es la herramienta de diagnostico. Sirve para comprobar
    que llegan los mensajes, que la clave cuadra y que el reloj no va desfasado
    (con TOTP, 30 s de desfase invalidan todos los codigos).
    """
    from alarm.config import ConfigError, load_config, load_env
    from alarm.services.ntfy_commands import CommandListener
    from alarm.services.totp import code_at

    head("Mando remoto: escuchar el tema")
    config_path, local_path, env_path = _config_paths(args)
    try:
        config = load_config(config_path, env=load_env(env_path), local=local_path)
    except ConfigError as exc:
        fail(str(exc))
        return 1

    topic = args.topic.strip() or config.remote_topic
    if not topic:
        fail("no hay tema de mando configurado; usa --topic o revisa la configuracion")
        return 1
    if not config.ntfy.configured:
        fail(f"faltan datos del servidor: {', '.join(config.ntfy.missing_fields)}")
        return 1

    line(f"  servidor : {config.ntfy.base_url}")
    line(f"  tema     : {topic}")
    line(f"  clave    : {'configurada' if config.remote.key else 'no'}")
    line(f"  TOTP     : {'configurado' if config.remote.totp_secret else 'no'}")
    if config.remote.uses_totp:
        line(f"  TOTP ahora: {code_at(config.remote.totp_secret)}  (compara con el movil)")
    if not config.remote.authenticates:
        warn("sin clave ni TOTP todo mensaje se rechazara")
    if not config.remote.enabled:
        info("el mando esta desactivado en la configuracion; se escucha igualmente")
    line()
    info("escuchando; pulsa Ctrl+C para parar")
    line()

    listener = CommandListener(
        config.ntfy,
        config.remote,
        topic,
        on_command=lambda command: ok(f"ORDEN VALIDA: {command.action} ({command.text})"),
        on_event=lambda kind, title, detail: warn(f"{title}: {detail}"),
    )
    listener.start()
    seen = 0
    deadline = time.time() + args.seconds if args.seconds else None
    try:
        while deadline is None or time.time() < deadline:
            time.sleep(0.25)
            entries = listener.snapshot().entries
            if len(entries) < seen:  # la tabla se recicla; se vuelve a empezar
                seen = 0
            for entry in entries[seen:]:
                if not args.all and entry.result == "no es una orden":
                    continue
                hour = time.strftime("%H:%M:%S", time.localtime(entry.at))
                result = entry.result if entry.ok else f"rechazada: {entry.result}"
                stamp = paint(result, "green" if entry.ok else "yellow")
                line(f"  {hour}  {entry.text:<14} {stamp}")
            seen = len(entries)
    except KeyboardInterrupt:
        line()
        info("parado a mano")
    finally:
        listener.stop()

    state = listener.snapshot()
    line()
    line(
        f"  recibidas: {state.received} · aceptadas: {state.accepted} · "
        f"rechazadas: {state.rejected}"
    )
    if state.last_at:
        line(f"  desfase de reloj con el servidor: {state.skew_s:+.1f} s")
        if abs(state.skew_s) > 15:
            warn("desfase alto: con TOTP, mas de 30 s invalida los codigos")
    if state.error:
        warn(f"ultimo error: {state.error}")
    return 0


# ---------------------------------------------------------------------------
# Subcomando: log
# ---------------------------------------------------------------------------

#: Raiz del proyecto, para resolver `[log] db_path` igual se lance desde donde
#: se lance la consola. La ventana usa `paths.storage_root()`; en desarrollo
#: ambos coinciden.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_HISTORY_DEFAULT = "data/alarm.db"

#: Color por etiqueta, el mismo criterio que el registro de la ventana: el
#: disparo y el error en rojo, lo que pide atencion sin ser un fallo en ambar.
_TAG_STYLE: dict[str, str] = {
    "triggered": "red",
    "error": "red",
    "rearming": "yellow",
    "rearmed": "yellow",
    "bounce": "yellow",
    "alarm": "blue",
    "info": "blue",
    "start": "dim",
    "end": "dim",
}

_RELATIVE_WHEN = re.compile(r"^-(\d+(?:\.\d+)?)([smhd])$")

_WHEN_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)


def _parse_when(raw: str, label: str) -> float:
    """Convierte lo que escribe el usuario en un instante (epoch local).

    Admite fecha suelta, fecha y hora, formato espanol y relativos (`-24h`,
    `-90m`, `-7d`), que son los que se teclean de verdad al buscar «que paso
    anoche».
    """
    text = raw.strip()
    match = _RELATIVE_WHEN.match(text.lower().replace(" ", ""))
    if match:
        factor = {"s": 1, "m": 60, "h": 3_600, "d": 86_400}[match.group(2)]
        return time.time() - float(match.group(1)) * factor
    for fmt in _WHEN_FORMATS:
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    raise ValueError(
        f"--{label} no es una fecha válida: «{raw}». Admite 2026-11-12, "
        "2026-11-12 18:30, 12/11/2026 o un relativo como -24h o -7d."
    )


def _history_path(args: argparse.Namespace) -> Path:
    """Ruta del historial: `--db`, o `[log] db_path` resuelto contra la raiz."""
    if args.db:
        return Path(args.db).expanduser().resolve()
    from alarm.config import ConfigError, load_config

    try:
        db_path = load_config().log.db_path
    except ConfigError as exc:
        warn(f"configuración inválida ({exc}); se usa {_HISTORY_DEFAULT}")
        db_path = _HISTORY_DEFAULT
    target = Path(db_path)
    return target if target.is_absolute() else (_PROJECT_ROOT / target).resolve()


def _print_tags(path: Path) -> int:
    from alarm.services.storage import tag_counts

    counts = tag_counts(path)
    if not counts:
        warn("el historial está vacío")
        return 0
    head("Etiquetas del registro")
    for tag, total in counts:
        line(f"  {paint(f'{tag:<12}', _TAG_STYLE.get(tag, ''))} {total:>7} filas")
    line()
    info("filtra con --tag ETIQUETA, por ejemplo --tag triggered")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    """Consulta el historial guardado y, si se pide, lo exporta a CSV.

    Lee en modo solo lectura: con la ventana abierta funciona igual y nunca
    crea una base vacía por una ruta equivocada, que es el fallo que hay que
    ver en vez de tapar.
    """
    from alarm.services.storage import count_rows, query_rows, write_csv

    path = _history_path(args)
    try:
        since = _parse_when(args.since, "since") if args.since else None
        until = _parse_when(args.until, "until") if args.until else None
    except ValueError as exc:
        fail(str(exc))
        return 1

    filters: dict[str, object] = {
        "since": since,
        "until": until,
        "tag": args.tag or None,
        "kind": args.kind or None,
        "text": args.text or None,
    }

    try:
        if args.tags:
            return _print_tags(path)
        if args.count:
            total = count_rows(path, **filters)  # type: ignore[arg-type]
            line(f"  {total} filas")
            return 0
        if args.csv:
            rows = query_rows(path, **filters)  # type: ignore[arg-type]
            if not rows:
                warn("no hay filas que cumplan esos filtros")
                return 1
            destino = Path(args.csv).expanduser()
            written = write_csv(destino, rows)
            ok(f"{written} filas exportadas a {destino}")
            return 0
        rows = query_rows(path, limit=args.limit, **filters)  # type: ignore[arg-type]
        total = count_rows(path, **filters)  # type: ignore[arg-type]
    except FileNotFoundError:
        fail(f"no hay historial en {path}")
        info("la ventana lo crea al arrancar; si usas otra ruta, pásala con --db")
        return 1
    except sqlite3.Error as exc:
        fail(f"no se pudo leer {path}: {exc}")
        return 1

    head("Registro de eventos")
    line(f"  fichero : {path}")
    line(f"  mostradas: {len(rows)} de {total}")
    if since is not None or until is not None:
        desde = time.strftime("%Y-%m-%d %H:%M", time.localtime(since)) if since else "el principio"
        hasta = time.strftime("%Y-%m-%d %H:%M", time.localtime(until)) if until else "ahora"
        line(f"  rango   : {desde} .. {hasta}")
    if not rows:
        line()
        warn("no hay filas que cumplan esos filtros")
        return 0
    line()
    for row in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row.ts))
        tag = paint(f"{row.tag:<12}", _TAG_STYLE.get(row.tag, ""))
        line(f"  {paint(stamp, 'dim')}  {tag} {row.kind} · {row.detail}")
    line()
    if total > len(rows):
        info(f"{total - len(rows)} filas más antiguas; usa --limit o acota con --since")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Copia completa de la configuracion, con o sin credenciales.

    Sirve para montar la alarma en otro PC sin repetir los dialogos: un solo
    fichero que lleva ajustes, puerto serie y secretos. La ventana hace lo mismo
    desde `Archivo`, pero este camino permite preparar un equipo por consola o
    por un script.
    """
    from alarm.backup import (
        EXTENSION,
        BackupError,
        build_backup,
        read_backup,
        restore,
        write_backup,
    )
    from alarm.config import ConfigError, load_config, load_env

    config_path, local_path, env_path = _config_paths(args)

    if args.backup_command == "export":
        try:
            config = load_config(config_path, env=load_env(env_path), local=local_path)
            backup = build_backup(config, include_secrets=not args.no_secrets, env_path=env_path)
        except ConfigError as exc:
            fail(f"configuración inválida: {exc}")
            return 1
        target = (
            Path(args.out).expanduser()
            if args.out
            else Path(f"copia-alarma-{time.strftime('%Y%m%d-%H%M')}{EXTENSION}")
        )
        if target.suffix.lower() != EXTENSION:
            target = target.with_suffix(EXTENSION)
        try:
            written = write_backup(backup, target)
        except OSError as exc:
            fail(f"no se pudo escribir {target}: {exc}")
            return 1
        head("Copia de la configuración")
        line(f"  fichero : {written}")
        line(f"  secciones: {', '.join(backup.sections)}")
        if backup.secrets:
            line(f"  secretos: {', '.join(backup.secret_keys)}")
            warn("contiene credenciales en claro: guárdala como guardarías una contraseña")
        else:
            line("  secretos: ninguno")
        return 0

    source = Path(args.source).expanduser()
    try:
        backup = read_backup(source)
    except BackupError as exc:
        fail(str(exc))
        return 1
    head("Copia encontrada")
    line(f"  fichero  : {source}")
    line(f"  creada   : {backup.created.replace('T', ' ') or 'sin fecha'}")
    line(f"  versión  : {backup.version or '?'} · equipo {backup.machine or '?'}")
    line(f"  secciones: {', '.join(backup.sections)}")
    line(f"  secretos : {', '.join(backup.secret_keys) or 'ninguno'}")
    line()
    if not args.yes and not _confirm("¿Restaurar esta copia en este PC?"):
        warn("cancelado; no se ha tocado nada")
        return 1

    try:
        restored = restore(backup, local_path=local_path, env_path=env_path)
    except OSError as exc:
        fail(f"no se pudo escribir la configuración: {exc}")
        return 1
    ok(f"configuración escrita en {restored.local_path}")
    if restored.env_path is not None:
        ok(f"{len(restored.secret_keys)} credencial(es) en {restored.env_path}")
    info("si la ventana está abierta, usa «Recargar configuración» o reiníciala")
    return 0


def _config_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    """Ficheros de configuracion a usar: los de siempre o los que diga `--config`."""
    from alarm.config import CONFIG_FILE, ENV_FILE, LOCAL_FILE

    config_path = Path(args.config).expanduser() if args.config else CONFIG_FILE
    local_path = Path(args.local).expanduser() if args.local else LOCAL_FILE
    env_path = Path(args.env).expanduser() if args.env else ENV_FILE
    return config_path, local_path, env_path


def _confirm(question: str) -> bool:
    """Pregunta por consola. Sin terminal interactiva, la respuesta es no."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{question} [s/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("s", "si", "sí", "y", "yes")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alarmctl",
        description="Utilidades de la alarma de presencia (Arduino Mega + HC-SR501).",
    )
    parser.add_argument("--sim", action="store_true", help="usar un dispositivo simulado")
    parser.add_argument("--port", help="puerto serie (por defecto, autodeteccion)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=12.0, help="espera de conexion")
    parser.add_argument("--sim-warmup", type=float, default=3.0)
    parser.add_argument("--sim-speed", type=float, default=4.0)
    parser.add_argument("--sim-seed", type=int, default=None)

    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("ports", help="lista los puertos serie").set_defaults(func=cmd_ports)

    subs.add_parser("info", help="identifica el dispositivo").set_defaults(func=cmd_info)

    mon = subs.add_parser("monitor", help="muestra eventos en vivo")
    mon.add_argument("--seconds", type=float, default=60.0)
    mon.add_argument("--skip-warmup", action="store_true")
    mon.add_argument("--show-hb", action="store_true", help="muestra cada latido")
    mon.set_defaults(func=cmd_monitor)

    cal = subs.add_parser("calibrate", help="asistente de ajuste del PIR")
    cal.add_argument("--quiet-s", type=float, default=30.0, help="duracion de la fase de reposo")
    cal.add_argument(
        "--motion-s", type=float, default=45.0, help="duracion de la fase de movimiento"
    )
    cal.add_argument("--skip-warmup", action="store_true", help="no esperar los 60 s del PIR")
    cal.add_argument("--no-wait", action="store_true", help="no pedir ENTER entre fases")
    cal.add_argument(
        "--prep-s",
        type=float,
        default=0.0,
        help="margen para salir de la estancia antes de medir (recomendado: 25)",
    )
    cal.add_argument("--beep", action="store_true", help="avisos sonoros en los cambios de fase")
    cal.set_defaults(func=cmd_calibrate)

    beep_cmd = subs.add_parser("beep", help="prueba el zumbador del dispositivo")
    beep_cmd.add_argument(
        "--name",
        default="test",
        choices=(
            "armed",
            "disarmed",
            "detect",
            "alarm",
            "error",
            "test",
            "tone",
            "exit",
            "entry",
            "stop",
        ),
        help="patron a reproducir; `tone` es un tono continuo de comprobacion",
    )
    beep_cmd.add_argument(
        "--ms",
        type=int,
        default=0,
        help="duracion en ms de la sirena, el tono o la cuenta atras (`exit`/`entry`)",
    )
    beep_cmd.add_argument(
        "--freq", type=int, default=2000, help="frecuencia en Hz del tono continuo"
    )
    beep_cmd.add_argument("--skip-warmup", action="store_true")
    beep_cmd.set_defaults(func=cmd_beep)

    subs.add_parser("notify-test", help="envia un correo de prueba").set_defaults(
        func=cmd_notify_test
    )

    ntfy_cmd = subs.add_parser("ntfy-test", help="envia una notificacion de prueba al movil")
    ntfy_cmd.add_argument(
        "--title", default="", help="titulo de la notificacion (por defecto, el configurado)"
    )
    ntfy_cmd.add_argument(
        "--config", metavar="RUTA", help="TOML base (por defecto config/alarm.toml)"
    )
    ntfy_cmd.add_argument(
        "--local", metavar="RUTA", help="TOML local (por defecto config/alarm.local.toml)"
    )
    ntfy_cmd.add_argument("--env", metavar="RUTA", help="fichero .env (por defecto .env)")
    ntfy_cmd.set_defaults(func=cmd_ntfy_test)

    send_cmd = subs.add_parser(
        "remote-send", help="publica una orden de mando remoto (para probar sin el movil)"
    )
    send_cmd.add_argument(
        "--action",
        required=True,
        choices=sorted(_REMOTE_WORDS),
        help="orden que se envia",
    )
    send_cmd.add_argument(
        "--key",
        default="",
        help="clave o codigo TOTP a mano (por defecto, el de la configuracion)",
    )
    send_cmd.add_argument("--topic", default="", help="tema de mando (por defecto, el configurado)")
    send_cmd.add_argument(
        "--config", metavar="RUTA", help="TOML base (por defecto config/alarm.toml)"
    )
    send_cmd.add_argument(
        "--local", metavar="RUTA", help="TOML local (por defecto config/alarm.local.toml)"
    )
    send_cmd.add_argument("--env", metavar="RUTA", help="fichero .env (por defecto .env)")
    send_cmd.set_defaults(func=cmd_remote_send)

    listen_cmd = subs.add_parser("remote-listen", help="escucha el tema de mando sin ejecutar nada")
    listen_cmd.add_argument(
        "--seconds", type=float, default=0.0, help="duracion de la escucha; 0 = hasta Ctrl+C"
    )
    listen_cmd.add_argument(
        "--all", action="store_true", help="muestra tambien los mensajes que no son ordenes"
    )
    listen_cmd.add_argument(
        "--topic", default="", help="tema de mando (por defecto, el configurado)"
    )
    listen_cmd.add_argument(
        "--config", metavar="RUTA", help="TOML base (por defecto config/alarm.toml)"
    )
    listen_cmd.add_argument(
        "--local", metavar="RUTA", help="TOML local (por defecto config/alarm.local.toml)"
    )
    listen_cmd.add_argument("--env", metavar="RUTA", help="fichero .env (por defecto .env)")
    listen_cmd.set_defaults(func=cmd_remote_listen)

    log_cmd = subs.add_parser("log", help="consulta y exporta el historial guardado")
    log_cmd.add_argument("--limit", type=int, default=30, help="filas a mostrar (0 = todas)")
    log_cmd.add_argument(
        "--since", metavar="FECHA", help="desde: 2026-11-12, 12/11/2026 18:30 o -24h"
    )
    log_cmd.add_argument("--until", metavar="FECHA", help="hasta, mismo formato que --since")
    log_cmd.add_argument(
        "--tag", help="etiqueta exacta: triggered, error, rearmed, info... (ver --tags)"
    )
    log_cmd.add_argument("--kind", help="texto contenido en el tipo de evento")
    log_cmd.add_argument("--text", help="texto contenido en el detalle")
    log_cmd.add_argument("--count", action="store_true", help="solo cuenta las filas")
    log_cmd.add_argument(
        "--csv", metavar="RUTA", help="exporta a CSV todo lo que cumpla los filtros"
    )
    log_cmd.add_argument("--tags", action="store_true", help="lista etiquetas y sus totales")
    log_cmd.add_argument(
        "--db", metavar="RUTA", help=f"historial a consultar (por defecto {_HISTORY_DEFAULT})"
    )
    log_cmd.set_defaults(func=cmd_log)

    backup_cmd = subs.add_parser(
        "backup", help="guarda o restaura la configuración completa (con credenciales)"
    )
    backup_sub = backup_cmd.add_subparsers(dest="backup_command", required=True)

    export_cmd = backup_sub.add_parser("export", help="escribe la copia en un fichero")
    export_cmd.add_argument(
        "--out",
        metavar="RUTA",
        default=None,
        help="fichero de salida (por defecto, copia-alarma-FECHA.alarmbackup)",
    )
    export_cmd.add_argument(
        "--no-secrets",
        action="store_true",
        help="no incluir contraseñas ni tokens; sirve para compartir la configuración",
    )
    export_cmd.set_defaults(func=cmd_backup)

    import_cmd = backup_sub.add_parser("import", help="aplica una copia en este PC")
    import_cmd.add_argument(
        "--from", dest="source", metavar="RUTA", required=True, help="copia a restaurar"
    )
    import_cmd.add_argument(
        "--yes", action="store_true", help="no pedir confirmación (para scripts)"
    )
    import_cmd.set_defaults(func=cmd_backup)

    for sub in (export_cmd, import_cmd):
        sub.add_argument(
            "--config", metavar="RUTA", help="TOML base (por defecto config/alarm.toml)"
        )
        sub.add_argument(
            "--local", metavar="RUTA", help="capa local (por defecto config/alarm.local.toml)"
        )
        sub.add_argument("--env", metavar="RUTA", help="fichero .env (por defecto .env)")

    return parser


def _glue_relative_dates(argv: list[str]) -> list[str]:
    """Convierte `--since -24h` en `--since=-24h`.

    argparse lee `-24h` como si fuera una opcion y responde «expected one
    argument», y escribir la espera relativa es lo mas comodo para preguntar
    «que paso ayer». Se unen antes de parsear, sin tocar el resto del argv.
    """
    result: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ("--since", "--until") and index + 1 < len(argv):
            value = argv[index + 1]
            if _RELATIVE_WHEN.match(value.strip().lower()):
                result.append(f"{item}={value}")
                index += 2
                continue
        result.append(item)
        index += 1
    return result


def _tolerar_consola() -> None:
    """Que un carácter que la consola no sepa dibujar no corte una tabla.

    Un `cmd.exe` con la página de códigos de fábrica en español (850) no tiene
    la raya ni las comillas tipográficas que usan los mensajes: sin esto,
    `alarmctl log` terminaría en `UnicodeEncodeError` a mitad de la salida y el
    usuario se quedaría sin las filas. Se sustituye el carácter suelto por «?»
    en vez de reventar; la codificación de la consola no se toca.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # una consola ya envuelta por otro programa
            continue
        with contextlib.suppress(ValueError, OSError):
            reconfigure(errors="replace")


def main(argv: list[str] | None = None) -> int:
    _tolerar_consola()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(_glue_relative_dates(raw))
    set_beeps_enabled(bool(getattr(args, "beep", False)))
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        line()
        info("cancelado por el usuario")
        return 130
    except Exception as exc:  # noqa: BLE001 - punto de entrada de consola
        fail(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
