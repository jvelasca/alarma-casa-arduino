"""Genera las capturas de la interfaz sin abrir una ventana real.

Se usa el modo `offscreen` de Qt, el mismo que en las pruebas. Asi las capturas
de `doc/img/` se pueden regenerar en cualquier maquina, incluida una sin
pantalla, y no dependen de que alguien recorte ventanas a mano.

Uso:

    .\\.venv\\Scripts\\python.exe tools\\ui_preview.py
    .\\.venv\\Scripts\\python.exe tools\\ui_preview.py --out doc/img

La configuracion de las capturas es la del ejemplo, con los canales
desactivados: no se envia nada ni se sintetiza voz al generarlas.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from _bootstrap import bootstrap

ROOT = bootstrap()

# Tiene que fijarse antes de crear QApplication. Se hace en el modulo para que
# tambien valga si alguien importa las funciones de aqui desde otro script.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from alarm.config import EXAMPLE_FILE, default_config, load_config  # noqa: E402
from alarm.services.ntfy_commands import RemoteEntry, RemoteState  # noqa: E402
from alarm.ui import theme  # noqa: E402
from alarm.ui.app import MainWindow  # noqa: E402
from alarm.ui.dialogs.about import AboutDialog  # noqa: E402
from alarm.ui.dialogs.buzzer import BuzzerDialog  # noqa: E402
from alarm.ui.dialogs.delays import DelaysDialog  # noqa: E402
from alarm.ui.dialogs.notifications import NotificationsDialog  # noqa: E402
from alarm.ui.dialogs.remote import RemoteDialog  # noqa: E402
from alarm.ui.dialogs.voice_config import VoiceConfigDialog  # noqa: E402


def preview_config(*, armed: bool = False):
    """Configuracion del ejemplo, sin canales activos ni voz."""
    base = default_config()
    if EXAMPLE_FILE.is_file():
        try:
            base = load_config(EXAMPLE_FILE, env={}, local=ROOT / "config" / "no-local.toml")
        except Exception:  # noqa: BLE001 - una captura no debe fallar por el TOML
            base = default_config()
    return replace(
        base,
        alarm=replace(base.alarm, armed_on_start=armed),
        tts=replace(base.tts, enabled=False),
        email=replace(base.email, enabled=False),
        sms=replace(base.sms, enabled=False),
        buzzer=replace(base.buzzer, enabled=False),
    )


def _pump(app: QApplication, rounds: int = 4) -> None:
    for _ in range(rounds):
        app.processEvents()


def _save(
    widget,
    path: Path,
    app: QApplication,
    *,
    resize_to_hint: bool = True,
    size: tuple[int, int] | None = None,
) -> None:
    assert widget is not None  # noqa: S101 - garantia de que Qt creo la ventana
    if size is not None:
        # Los dialogos con pestañas largas se capturan con el alto que pide cada
        # pestana: `adjustSize` los recorta a dos tercios de la pantalla y la
        # captura saldria con barra de desplazamiento.
        widget.resize(*size)
    elif resize_to_hint:
        # Los dialogos se ajustan a su contenido; la ventana principal no,
        # porque encogeria al minimo y las filas de la tarjeta se montarian.
        widget.adjustSize()
    widget.show()
    _pump(app)
    pixmap = widget.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"no se pudo guardar {path}")
    widget.close()
    widget.deleteLater()
    _pump(app, 2)
    print(f"  {path}")


def _paint_connected(window) -> None:
    """Deja la captura como si la placa estuviera respondiendo, sin hardware.

    El boton verde «Conectado» es la pieza que el usuario pidio ver: una captura
    con el boton gris «Conectar» no lo documenta. El panel del sensor se rellena
    con detecciones para que la franja y las barras no salgan vacias.
    """
    window._link_name = "COM5"
    window._paint_connection_state("connected")
    window._link_status.setText("● Conectado · latido hace 0,4 s")
    window._link_status.setStyleSheet(f"color: {theme.OK}; font-weight: 600;")
    window._device_label.setText("Arduino Mega 2560 · fw 1.0")
    # La captura se toma a mitad del calentamiento para que se vea la barra.
    window._warmup_total_ms = 60000
    window._warmup_base_ms = 26000
    window._warmup_synced_at = time.monotonic()
    window._warmup_bar.setVisible(True)
    window._paint_warmup()

    sensor = window._sensor
    sensor.simulate()
    sensor.beat()

    for tag, kind, detail in (
        ("start", "Movimiento", "Detección iniciada"),
        ("end", "Fin de pulso", "duración 2700 ms"),
        ("info", "Aviso", "correo en cola: Presencia detectada"),
    ):
        window._log.add_row(tag, kind, detail, scroll=False)
    window._log_footer.setText("Mostrando las últimas 3 de 128 · retención 1 año / 100.000 eventos")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capturas de la interfaz a PNG.")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "doc" / "img",
        help="carpeta de salida (por defecto doc/img)",
    )
    args = parser.parse_args(argv)
    out = args.out if args.out.is_absolute() else ROOT / args.out

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(sys.argv[:1])
    theme.apply(app)

    print(f"Capturas en {out}")

    window = MainWindow(config=preview_config(armed=True), autoconnect=False)
    window.resize(1020, 760)
    _paint_connected(window)
    _save(window, out / "main-window.png", app, resize_to_hint=False)

    # Mismo montaje pero recien armado: el reloj grande de la cuenta atras es la
    # pieza que necesita documentarse, y en reposo no se ve.
    arming = MainWindow(config=preview_config(), autoconnect=False)
    arming.resize(1020, 760)
    _paint_connected(arming)
    arming.arm()
    _save(arming, out / "main-window-retardo.png", app, resize_to_hint=False)

    empty = preview_config()
    voices = VoiceConfigDialog(
        None,
        config=empty,
        on_save=lambda _config: None,
        on_try=lambda *_args: None,
    )
    voices._table.selectRow(2)
    _save(voices, out / "dialog-voice-config.png", app)

    buzzer = BuzzerDialog(
        None,
        config=empty,
        on_save=lambda _config: None,
        on_pattern=lambda *_args: True,
        on_tone=lambda *_args: True,
        connected=True,
    )
    _save(buzzer, out / "dialog-buzzer.png", app)

    delays = DelaysDialog(
        None,
        config=empty,
        on_save=lambda _config: None,
        on_preview=lambda *_args: True,
        connected=True,
    )
    _save(delays, out / "dialog-delays.png", app)

    notifications = NotificationsDialog(
        None,
        config=empty,
        on_save=lambda _config, _secrets: None,
        on_test_email=lambda _config: None,
        on_test_sms=lambda _config: None,
    )
    _save(notifications, out / "dialog-notifications.png", app, size=(900, 860))

    sms = NotificationsDialog(
        None,
        config=empty,
        on_save=lambda _config, _secrets: None,
        on_test_email=lambda _config: None,
        on_test_sms=lambda _config: None,
    )
    sms._tabs.setCurrentIndex(1)
    sms._sms_status.setText("Falta: cuenta SID, token de autenticación y número remitente.")
    sms._sms_status.setStyleSheet(f"color: {theme.WARN};")
    _save(sms, out / "dialog-sms.png", app, size=(900, 940))

    # La pestana del movil, con un tema ya generado: es el estado en el que se
    # ve el aviso de que en ntfy.sh el tema no es privado.
    ntfy = NotificationsDialog(
        None,
        config=empty,
        on_save=lambda _config, _secrets: None,
        on_test_email=lambda _config: None,
        on_test_sms=lambda _config: None,
        on_test_ntfy=lambda _config: None,
    )
    ntfy._tabs.setCurrentIndex(2)
    ntfy._ntfy_topic.setText("alarma-9fj2k_x7qp1a")
    ntfy._refresh_ntfy_status()
    _save(ntfy, out / "dialog-ntfy.png", app, size=(900, 720))

    # El mando remoto: clave generada, estado escuchando y unas ordenes de
    # ejemplo. El listener falso evita abrir red al generar la captura.
    remote_cfg = replace(
        empty,
        ntfy=replace(empty.ntfy, enabled=True, topic="alarma-9fj2k_x7qp1a"),
        remote=replace(empty.remote, enabled=True, key="mando-7f3k9x-2a8p"),
    )
    now = time.time()

    def _remote_snapshot() -> RemoteState:
        return RemoteState(
            listening=True,
            connected=True,
            since=now - 540,
            topic=remote_cfg.remote_topic,
            received=6,
            accepted=5,
            rejected=1,
            last_text="clave consultar",
            last_result="Estado enviado al móvil",
            last_at=now - 35,
            entries=(
                RemoteEntry(
                    at=now - 35,
                    text="clave consultar",
                    result="Estado enviado al móvil",
                    ok=True,
                ),
                RemoteEntry(
                    at=now - 240,
                    text="clave armar",
                    result="Armada. Vigila en 45 s",
                    ok=True,
                ),
                RemoteEntry(
                    at=now - 420,
                    text="000000 desarmar",
                    result="Credencial incorrecta",
                    ok=False,
                ),
            ),
        )

    remote = RemoteDialog(
        None,
        config=remote_cfg,
        on_save=lambda _config, _secrets: None,
        snapshot=_remote_snapshot,
    )
    _save(remote, out / "dialog-remote.png", app, size=(860, 820))

    about = AboutDialog(None, simulated=True)
    _save(about, out / "dialog-about.png", app)
    QCoreApplication.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
