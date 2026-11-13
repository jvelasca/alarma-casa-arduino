"""Coherencia de la version: un solo numero, escrito en tres sitios.

`pyproject.toml` es lo que se publica, `alarm.__version__` es lo que se ve en la
ventana y en `--version`, y `CHANGELOG.md` es lo que se lee para saber que hay.
Si los tres no cuentan lo mismo, la version deja de servir para nada: se sube en
un sitio, se olvida en otro y una copia de seguridad acaba diciendo que viene de
una version que no existe.

Se comprueba aqui y no en un script suelto porque asi la integracion continua lo
verifica en cada push, sin que nadie tenga que acordarse.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import alarm

ROOT = Path(__file__).resolve().parents[1]

# SemVer 2.0.0: MAYOR.MENOR.PARCHE, con prepublicacion y metadatos opcionales.
_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


def _pyproject() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_la_version_es_semver() -> None:
    assert _SEMVER.match(alarm.__version__), alarm.__version__


def test_el_paquete_y_el_pyproject_coinciden() -> None:
    proyecto = _pyproject()["project"]
    assert isinstance(proyecto, dict)  # noqa: S101 - garantia del esquema
    assert proyecto["version"] == alarm.__version__


def test_el_changelog_documenta_la_version_actual() -> None:
    texto = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{alarm.__version__}]" in texto
    # La entrada de la version en curso no puede quedar como «sin publicar»:
    # eso significa que se subio el numero y no se conto que cambio.
    assert f"## [{alarm.__version__}] — sin publicar" not in texto


def test_los_cambios_sin_publicar_van_delante() -> None:
    texto = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert texto.index("## [Sin publicar]") < texto.index(f"## [{alarm.__version__}]")
