"""Configuracion comun de las pruebas.

Qt necesita una sola `QApplication` por proceso y, en una maquina sin sesion
grafica (CI, servidor, terminal remota), el plugin `offscreen`. Ambas cosas se
fijan aqui antes de importar PyQt6, que decide el plugin al cargarse.

La ventana no se abre nunca de verdad: `show()` sobre el plugin offscreen no
pinta, pero construye toda la jerarquia de widgets.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp() -> Iterator[object]:
    """Aplicacion Qt compartida por todas las pruebas."""
    from PyQt6.QtWidgets import QApplication

    from alarm.ui import theme

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    theme.apply(app)  # type: ignore[arg-type]
    yield app
