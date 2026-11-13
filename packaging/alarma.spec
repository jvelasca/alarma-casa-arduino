# -*- mode: python ; coding: utf-8 -*-
r"""Empaquetado de la alarma en un unico `.exe`.

    scripts\build.ps1

Un solo fichero, sin instalador ni Python en el equipo destino: se copia, se
abre y la aplicacion escribe `config/` y `data/` a su lado. Eso es lo que hace
que la copia de seguridad completa (`Archivo → Restaurar copia completa`) deje el
programa funcionando igual que en el PC de origen.

Puntos que no son evidentes:

- `launcher.py` existe porque `alarm/__main__.py` usa imports relativos.
- `config/alarm.example.toml` viaja como dato: sin el, un equipo nuevo no tiene
  plantilla que copiar a `config/alarm.toml`.
- `build/alarma.ico` y `build/version_info.txt` los genera
  `tools/make_assets.py` antes de llamar a PyInstaller, para que el icono y la
  version no se queden atras.
- Un `.exe` de PyInstaller se descomprime en `%TEMP%` en cada arranque: eso
  explica los primeros segundos y por que la configuracion **no** se guarda ahi
  (ver `src/alarm/paths.py`).
"""

import sys
from pathlib import Path

# `SPECPATH` lo define PyInstaller al cargar este fichero.
ROOT = Path(SPECPATH).resolve().parent  # noqa: F821
SRC = ROOT / "src"
BUILD = ROOT / "build"
ICON = BUILD / "alarma.ico"
VERSION_FILE = BUILD / "version_info.txt"
EXAMPLE = ROOT / "config" / "alarm.example.toml"

if not ICON.is_file() or not VERSION_FILE.is_file():
    raise SystemExit(
        "Faltan los recursos del ejecutable. Ejecuta antes:\n"
        f"  {sys.executable} tools/make_assets.py"
    )
if not EXAMPLE.is_file():
    raise SystemExit(f"No existe el TOML de ejemplo: {EXAMPLE}")

datas = [(str(EXAMPLE), "config")]

hiddenimports = [
    # pyserial importa el modulo de su plataforma desde dentro de funciones.
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
    # Qt solo: el resto de modulos de PyQt6 no se usan.
    "PyQt6.QtSvg",
]

excludes = [
    "tkinter",
    "unittest",
    "pytest",
    "pydoc",
    "pdb",
    "doctest",
    # Nada de la aplicacion usa red de Qt, impresion ni multimedia.
    "PyQt6.QtNetwork",
    "PyQt6.QtQml",
    "PyQt6.QtQuick",
    "PyQt6.QtMultimedia",
    "PyQt6.QtPrintSupport",
    "PyQt6.QtOpenGL",
    "PyQt6.QtWebEngineCore",
]

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AlarmaCasa",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
    version=str(VERSION_FILE),
)
