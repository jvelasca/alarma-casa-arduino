"""Pruebas del listado de voces SAPI.

`installed_voices()` arranca PowerShell, asi que en las pruebas se sustituye
`subprocess.run`. Lo que se comprueba no es que Windows tenga voces, sino que:

- el resultado se memoriza (abrir el dialogo dos veces no lanza dos procesos);
- un fallo no se memoriza, para poder reintentar;
- no se propaga ninguna excepcion al dialogo.
"""

from __future__ import annotations

import subprocess

import pytest

from alarm.services import tts


class FakeCompleted:
    def __init__(self, stdout: bytes = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = b""
        self.returncode = returncode


@pytest.fixture(autouse=True)
def sin_cache_de_voces(monkeypatch: pytest.MonkeyPatch):
    """Cada prueba parte de una cache vacia y la restaura al terminar."""
    monkeypatch.setattr(tts, "_voices_cache", None)
    yield
    monkeypatch.setattr(tts, "_voices_cache", None)


def test_devuelve_las_voces_instaladas(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def fake_run(*args: object, **kwargs: object) -> FakeCompleted:
        calls.append(args)
        return FakeCompleted(b"Microsoft Helena Desktop\nMicrosoft Pablo\n\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert tts.installed_voices() == ["Microsoft Helena Desktop", "Microsoft Pablo"]
    assert len(calls) == 1


def test_memoriza_el_resultado(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_run(*args: object, **kwargs: object) -> FakeCompleted:
        calls["n"] += 1
        return FakeCompleted(b"Voz unica\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert tts.installed_voices() == ["Voz unica"]
    assert tts.installed_voices() == ["Voz unica"]
    assert calls["n"] == 1, "la segunda apertura del dialogo no debe arrancar PowerShell"


def test_devuelve_copia_para_no_alterar_la_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted(b"A\nB\n"))
    voces = tts.installed_voices()
    voces.clear()
    assert tts.installed_voices() == ["A", "B"]


def test_un_fallo_no_se_memoriza(monkeypatch: pytest.MonkeyPatch) -> None:
    intentos = {"n": 0}

    def fake_run(*args: object, **kwargs: object) -> FakeCompleted:
        intentos["n"] += 1
        if intentos["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="powershell", timeout=15)
        return FakeCompleted(b"Voz recuperada\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert tts.installed_voices() == []
    assert tts.installed_voices() == ["Voz recuperada"]


def test_un_codigo_de_error_no_revienta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeCompleted(b"", returncode=1))
    assert tts.installed_voices() == []


def test_sin_powershell_devuelve_lista_vacia(monkeypatch: pytest.MonkeyPatch) -> None:
    def explota(*args: object, **kwargs: object) -> FakeCompleted:
        raise OSError("powershell no encontrado")

    monkeypatch.setattr(subprocess, "run", explota)
    assert tts.installed_voices() == []
