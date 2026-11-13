"""Pruebas de `alarmctl remote-send` y `alarmctl remote-listen`.

Se lanzan como proceso aparte, como el resto de subcomandos: lo que importa es
que la orden de consola responda con un mensaje util sin tocar la red. El envio
real se prueba a mano contra ntfy; aqui se comprueba la construccion del
mensaje, que es donde una errata en la clave o en la palabra rompe el mando.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from _entorno import entorno_utf8

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "alarmctl.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=entorno_utf8(),
        cwd=ROOT,
        timeout=120,
    )


def _paths(tmp_path: Path) -> list[str]:
    return [
        "--config", str(tmp_path / "alarm.toml"),
        "--local", str(tmp_path / "alarm.local.toml"),
        "--env", str(tmp_path / ".env"),
    ]  # fmt: skip


def _config(tmp_path: Path, extra: str = "") -> None:
    (tmp_path / "alarm.toml").write_text(
        "\n".join(
            [
                "[notify.ntfy]",
                'server = "http://127.0.0.1:9"',
                'topic = "casa-abc123"',
                "timeout_s = 1",
                "retries = 0",
                "",
                "[remote]",
                "enabled = true",
                extra,
                "",
            ]
        ),
        encoding="utf-8",
    )


def _env(tmp_path: Path, content: str) -> None:
    (tmp_path / ".env").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# remote-send
# ---------------------------------------------------------------------------


def test_sin_configuracion_avisa_y_no_envia(tmp_path: Path) -> None:
    result = _run("remote-send", "--action", "armar", *_paths(tmp_path))

    assert result.returncode == 1
    assert "tema" in result.stdout.lower()


def test_muestra_la_orden_y_el_tema_efectivo(tmp_path: Path) -> None:
    _config(tmp_path)
    _env(tmp_path, "ALARM_REMOTE_KEY=clave-de-prueba-1234\n")

    # El puerto 9 esta cerrado: falla al instante, sin depender de internet.
    result = _run("remote-send", "--action", "desarmar", *_paths(tmp_path))

    assert result.returncode == 1
    assert "casa-abc123-mando" in result.stdout
    assert "desarmar" in result.stdout
    assert "clave de la configuracion" in result.stdout


def test_una_clave_a_mano_se_usa_tal_cual(tmp_path: Path) -> None:
    _config(tmp_path)

    result = _run(
        "remote-send",
        "--action",
        "consultar",
        "--key",
        "999999",
        *_paths(tmp_path),
    )

    assert result.returncode == 1  # falla la red, no la construccion
    assert "clave indicada a mano" in result.stdout


def test_el_tema_se_puede_forzar(tmp_path: Path) -> None:
    _config(tmp_path)
    _env(tmp_path, "ALARM_REMOTE_KEY=clave-de-prueba-1234\n")

    result = _run(
        "remote-send",
        "--action",
        "armar",
        "--topic",
        "tema-de-prueba",
        *_paths(tmp_path),
    )

    assert result.returncode == 1
    assert "tema-de-prueba" in result.stdout


def test_sin_credenciales_avisa_de_que_sera_rechazada(tmp_path: Path) -> None:
    _config(tmp_path)

    result = _run("remote-send", "--action", "armar", *_paths(tmp_path))

    assert result.returncode == 1
    assert "sin credencial" in result.stdout


# ---------------------------------------------------------------------------
# remote-listen
# ---------------------------------------------------------------------------


def test_escuchar_sin_tema_avisa(tmp_path: Path) -> None:
    result = _run("remote-listen", "--seconds", "1", *_paths(tmp_path))

    assert result.returncode == 1
    assert "tema de mando" in result.stdout


def test_escuchar_muestra_el_tema_y_la_clave_configurada(tmp_path: Path) -> None:
    _config(tmp_path)
    _env(tmp_path, "ALARM_REMOTE_KEY=clave-de-prueba-1234\n")

    result = _run("remote-listen", "--seconds", "0.3", *_paths(tmp_path))

    assert result.returncode == 0
    assert "casa-abc123-mando" in result.stdout
    assert "clave    : configurada" in result.stdout
    assert "no ejecuta" in result.stdout or "escuchando" in result.stdout


def test_escuchar_con_totp_muestra_el_codigo_del_instante(tmp_path: Path) -> None:
    _config(tmp_path)
    # Secreto base32 valido de 32 caracteres.
    _env(tmp_path, "ALARM_REMOTE_TOTP=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP\n")

    result = _run("remote-listen", "--seconds", "0.3", *_paths(tmp_path))

    assert result.returncode == 0
    assert "TOTP ahora" in result.stdout
    assert "compara con el movil" in result.stdout
