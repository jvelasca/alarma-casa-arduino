#!/usr/bin/env python3
"""Genera los recursos que PyInstaller necesita, a partir del codigo.

Dos ficheros, siempre derivados de la fuente de verdad para que no se queden
atras:

- `build/alarma.ico`: el mismo icono de la ventana (`icons.app_pixmap`), para el
  ejecutable, el acceso directo y el Explorador.
- `build/version_info.txt`: el recurso de version de Windows, con la version de
  `alarm.__version__` y el nombre del producto.

Se ejecuta solo desde `scripts/build.ps1`; las pruebas no lo necesitan. Qt se
arranca en modo `offscreen` porque aqui no hay ventana que pintar.

    python tools/make_assets.py
    python tools/make_assets.py --out otra/carpeta
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PRODUCT = "Alarma de presencia"
COMPANY = "Proyecto personal"
DESCRIPTION = "Alarma de presencia con Arduino Mega 2560 y HC-SR501"
EXE_NAME = "AlarmaCasa"

VERSION_INFO = """\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={tuple},
    prodvers={tuple},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040a04b0',
        [StringStruct('CompanyName', '{company}'),
         StringStruct('FileDescription', '{description}'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', '{exe}'),
         StringStruct('LegalCopyright', ''),
         StringStruct('OriginalFilename', '{exe}.exe'),
         StringStruct('ProductName', '{product}'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1034, 1200])])
  ]
)
"""


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """`0.1.0` -> `(0, 1, 0, 0)`, que es lo que espera el recurso de Windows."""
    parts: list[int] = []
    for piece in version.split(".")[:4]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])  # type: ignore[return-value]


def write_icon(target: Path) -> Path:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    instance = QApplication.instance()
    app = instance if isinstance(instance, QApplication) else QApplication([])
    from alarm.ui import icons

    target.parent.mkdir(parents=True, exist_ok=True)
    if not icons.app_pixmap(256).save(str(target), "ICO"):
        raise SystemExit(f"No se pudo escribir el icono en {target}")
    del app
    return target


def write_version_info(target: Path, version: str) -> Path:
    numbers = version_tuple(version)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        VERSION_INFO.format(
            tuple=numbers,
            company=COMPANY,
            description=DESCRIPTION,
            version=version,
            exe=EXE_NAME,
            product=PRODUCT,
        ),
        encoding="utf-8",
    )
    return target


def main(argv: list[str] | None = None) -> int:
    from alarm import __version__

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", default=str(ROOT / "build"), help="carpeta de salida (por defecto build/)"
    )
    args = parser.parse_args(argv)
    out = Path(args.out)

    icon = write_icon(out / "alarma.ico")
    info = write_version_info(out / "version_info.txt", __version__)
    print(f"icono   : {icon} ({icon.stat().st_size} bytes)")
    print(f"version : {info} ({__version__})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
