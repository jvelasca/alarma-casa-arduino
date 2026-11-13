"""Arranque del ejecutable de un solo fichero.

PyInstaller necesita un script de entrada que no sea `alarm/__main__.py`: ese
modulo usa imports relativos (`from .config import ...`) y, ejecutado como
script suelto, no tiene paquete padre. Este lanzador importa el paquete ya
empaquetado y llama a su `main()`.

Su otra mision es no dejar morir la aplicacion en silencio. Sin consola
(`console=False`), una excepcion antes de que exista la ventana no imprime nada
en ningun sitio: el usuario hace doble clic, no pasa nada y no hay forma de
saber por que. Aqui se registra en `data/arranque.log` junto al ejecutable y se
avisa con un cuadro de dialogo.
"""

from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path


def _log_path() -> Path:
    """Mismo sitio que usa `paths.storage_root()`, sin depender de el."""
    frozen = getattr(sys, "frozen", False)
    base = Path(sys.executable).resolve().parent if frozen else Path.cwd()
    target = base / "data"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".escritura"
        probe.touch()
        probe.unlink()
    except OSError:
        target = Path.home() / "AlarmaCasa" / "data"
        target.mkdir(parents=True, exist_ok=True)
    return target / "arranque.log"


def _fatal(detail: str) -> None:
    try:
        with _log_path().open("a", encoding="utf-8") as handle:
            handle.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} fallo al arrancar\n")
            handle.write(detail.rstrip() + "\n")
    except OSError:
        pass
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        instance = QApplication.instance()
        app = instance if isinstance(instance, QApplication) else QApplication([])
        QMessageBox.critical(
            None,
            "La alarma no pudo arrancar",
            "Se ha producido un error inesperado al iniciar la aplicación.\n\n"
            f"{detail.strip().splitlines()[-1] if detail.strip() else ''}\n\n"
            f"Detalle completo en:\n{_log_path()}",
        )
        del app
    except Exception:  # noqa: BLE001 - si Qt tampoco arranca, queda el log
        pass


def main() -> int:
    try:
        from alarm.__main__ import main as run_app
    except BaseException:  # noqa: BLE001 - informar de cualquier fallo de importacion
        _fatal(traceback.format_exc())
        return 1
    try:
        return int(run_app())
    except SystemExit:  # pragma: no cover - argparse --help/--version
        raise
    except BaseException:  # noqa: BLE001
        _fatal(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
