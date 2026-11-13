"""Rutas de la aplicacion dentro y fuera del ejecutable empaquetado.

Lo que se comprueba es la decision, no Windows: que en desarrollo todo cuelgue
del directorio actual, que los recursos se busquen en la raiz del repositorio y
que, si la carpeta del ejecutable no admite escritura, los datos se vayan al
perfil del usuario en vez de perderse.

`paths` memoriza la raiz (`storage_root`), asi que cada prueba la olvida antes y
despues con `forget()`.
"""

from __future__ import annotations

import sys

import pytest

from alarm import paths


@pytest.fixture(autouse=True)
def _olvidar_raiz():
    paths.forget()
    yield
    paths.forget()


def test_en_desarrollo_la_raiz_es_el_directorio_actual(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delattr(sys, "frozen", raising=False)

    assert paths.is_frozen() is False
    assert paths.app_root() == tmp_path.resolve()


def test_empaquetado_la_raiz_es_la_carpeta_del_exe(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "AlarmaCasa.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    assert paths.is_frozen() is True
    assert paths.app_root() == tmp_path.resolve()


def test_los_recursos_se_leen_de_la_carpeta_temporal_del_exe(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert paths.bundle_root() == tmp_path
    assert paths.resource_path("config", "alarm.example.toml") == (
        tmp_path / "config" / "alarm.example.toml"
    )


def test_el_ejemplo_existe_en_el_repositorio() -> None:
    """Si esta ruta cambia, el `.exe` se queda sin plantilla y no arranca igual."""
    example = paths.resource_path("config", "alarm.example.toml")
    assert example.is_file(), f"no se encontro el TOML de ejemplo en {example}"


def test_si_la_carpeta_no_admite_escritura_se_usa_el_perfil(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "_writable", lambda directory: False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    root = paths.storage_root()

    assert root == tmp_path / paths.APP_DIR_NAME
    assert root.is_dir()
    assert paths.fallback_used() is True


def test_si_la_carpeta_admite_escritura_no_hay_respaldo(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "_writable", lambda directory: True)

    assert paths.storage_root() == tmp_path.resolve()
    assert paths.fallback_used() is False
