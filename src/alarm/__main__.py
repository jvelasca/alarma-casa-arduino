"""Punto de entrada de la aplicacion.

    python -m alarm              ventana con el dispositivo conectado
    python -m alarm --sim        ventana con el dispositivo simulado
    python -m alarm --version    version y carpeta de datos

Es lo que lanza la configuracion de depuracion de `.vscode/launch.json`, de
modo que F5 en Cursor abre directamente la aplicacion, y lo que ejecuta el
`.exe` de un solo fichero (`packaging/launcher.py`).

En el ejecutable no hay consola: `sys.stderr` puede ser `None` y escribir en el
no llega a ningun sitio. Los fallos de arranque se registran en
`data/arranque.log` y, si Qt esta disponible, se muestran en un cuadro de
dialogo; un doble clic que no hace nada es el peor mensaje de error posible.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alarm",
        description="Alarma de presencia: Arduino Mega 2560 + HC-SR501.",
    )
    parser.add_argument(
        "--sim", action="store_true", help="usar un dispositivo simulado en lugar del real"
    )
    parser.add_argument("--port", help="puerto serie (por defecto, autodeteccion)")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument(
        "--sim-speed", type=float, default=1.0, help="aceleracion del tiempo simulado"
    )
    parser.add_argument(
        "--log", default="INFO", help="nivel de registro (DEBUG, INFO, WARNING, ERROR)"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Alarma de presencia {__version__}",
    )
    return parser


def _log_startup_failure(message: str) -> Path | None:
    """Deja constancia del fallo en `data/arranque.log`."""
    from .paths import storage_root

    target = storage_root() / "data" / "arranque.log"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:  # pragma: no cover - permisos
        return None
    return target


def _report(title: str, message: str) -> None:
    """Avisa de un fallo de arranque sin consola."""
    _log_startup_failure(f"{title}: {message}")
    if sys.stderr is not None:
        print(f"{title}: {message}", file=sys.stderr)
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        instance = QApplication.instance()
        gui = instance if isinstance(instance, QApplication) else QApplication([])
        QMessageBox.critical(None, title, message)
        del gui
    except Exception:  # noqa: BLE001 - si Qt tampoco arranca, queda el log
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        from .config import ConfigError, ensure_config_file, load_config
        from .ui.app import run
    except ImportError as exc:  # pragma: no cover - entorno sin PyQt6
        _report(
            "No se pudo cargar la interfaz gráfica",
            f"{exc}\n\nLa ventana usa PyQt6. Ejecuta .\\scripts\\setup.ps1 o instala "
            "las dependencias con: pip install -e .",
        )
        return 1

    try:
        ensure_config_file()
        config = load_config()
    except ConfigError as exc:
        _report(
            "Configuración inválida",
            f"{exc}\n\nRevisa config/alarm.toml. Si quieres empezar de cero, borra "
            "config/alarm.local.toml y el programa vuelve a los valores del ejemplo.",
        )
        return 1

    return run(
        simulated=args.sim,
        port=args.port,
        baud=args.baud,
        sim_speed=args.sim_speed,
        config=config,
    )


if __name__ == "__main__":
    raise SystemExit(main())
