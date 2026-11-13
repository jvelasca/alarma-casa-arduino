"""Entorno comun para lanzar la consola `alarmctl` como proceso aparte.

En Windows, un `python` hijo toma la codificacion de la consola del equipo
(cp1252, cp850...) y aqui su salida se captura por una tuberia y se lee como
UTF-8. Sin fijarla, cada acento y cada raya llegan como bytes que no son UTF-8
y las pruebas pasan o fallan segun la pagina de codigos del equipo, que es
justo la clase de fallo que solo aparece en el servidor de integracion.
"""

from __future__ import annotations

import os


def entorno_utf8() -> dict[str, str]:
    """Copia del entorno con la salida del hijo forzada a UTF-8."""
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}
