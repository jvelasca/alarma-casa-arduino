"""Utilidades comunes de los scripts de linea de comandos."""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def bootstrap() -> pathlib.Path:
    """Permite ejecutar los scripts sin instalar el paquete."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    return ROOT
