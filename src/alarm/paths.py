"""Rutas de la aplicacion, dentro del repositorio o dentro del ejecutable.

En desarrollo la aplicacion vive en el repositorio y escribe `config/`, `data/`
y `.env` junto a los fuentes, que es lo comodo para editar, versionar y depurar.
En el ejecutable de un solo fichero las cosas cambian: PyInstaller lo descomprime
en una carpeta temporal y `__file__` apunta alli. Esa carpeta se borra al cerrar,
asi que guardar la configuracion «junto al codigo» la perderia en cada arranque.

Por eso se distinguen dos raices:

* `app_root()`: donde vive el ejecutable; en desarrollo, el directorio actual.
* `bundle_root()`: donde estan los recursos de solo lectura empaquetados (el TOML
  de ejemplo). Dentro del `.exe` es la carpeta temporal de PyInstaller.

`storage_root()` es `app_root()` salvo que no se pueda escribir en el: un `.exe`
metido en `C:\\Program Files` no puede crear `config/` a su lado, y arrancar sin
poder guardar nada es peor que guardar en otro sitio. En ese caso cae a
`%LOCALAPPDATA%\\AlarmaCasa` y `fallback_used()` lo dice, para que la ventana
pueda avisar en vez de que el usuario se pregunte donde fueron sus ajustes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

#: Nombre de la carpeta de respaldo en el perfil del usuario.
APP_DIR_NAME = "AlarmaCasa"


def is_frozen() -> bool:
    """True si corremos desde un ejecutable empaquetado con PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Carpeta de trabajo de la aplicacion.

    Con el `.exe`, la carpeta que lo contiene: el usuario copia un solo fichero
    a una carpeta y sus datos viven ahi. En desarrollo, el directorio actual,
    que es el repositorio cuando se lanza con `run.ps1` o F5.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def bundle_root() -> Path:
    """Carpeta de los recursos de solo lectura que viajan dentro del ejecutable."""
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        return Path(str(mei))
    # src/alarm/paths.py -> raiz del repositorio
    return Path(__file__).resolve().parents[2]


def resource_path(*parts: str) -> Path:
    """Ruta de un recurso empaquetado, resuelta dentro o fuera del `.exe`."""
    return bundle_root().joinpath(*parts)


def _writable(directory: Path) -> bool:
    """Comprueba escribiendo de verdad: los permisos de Windows no se leen."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".escritura-", dir=directory):
            pass
    except OSError:
        return False
    return True


@lru_cache(maxsize=1)
def storage_root() -> Path:
    """Carpeta donde viven `config/`, `data/` y `.env`."""
    base = app_root()
    if _writable(base):
        return base
    profile = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    fallback = (Path(profile) if profile else Path.home()) / APP_DIR_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def fallback_used() -> bool:
    """True si los datos acabaron fuera de la carpeta del ejecutable."""
    return storage_root() != app_root()


def forget() -> None:
    """Olvida la raiz memorizada. Solo lo usan las pruebas."""
    storage_root.cache_clear()


__all__ = [
    "APP_DIR_NAME",
    "app_root",
    "bundle_root",
    "fallback_used",
    "forget",
    "is_frozen",
    "resource_path",
    "storage_root",
]
