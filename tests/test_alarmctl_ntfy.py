"""Pruebas de `alarmctl ntfy-test`.

Se lanza como proceso aparte, igual que `backup` y `log`: lo que importa es que
la orden que teclea el usuario responda con un mensaje util y un codigo de
salida claro. No se toca la red: los casos probados fallan antes de enviar.
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
        [sys.executable, str(TOOL), "ntfy-test", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=entorno_utf8(),
        cwd=ROOT,
        timeout=120,
    )


def _paths(tmp_path: Path) -> list[str]:
    """Rutas de configuracion y `.env` aisladas en el directorio temporal."""
    return [
        "--config", str(tmp_path / "alarm.toml"),
        "--local", str(tmp_path / "alarm.local.toml"),
        "--env", str(tmp_path / ".env"),
    ]  # fmt: skip


def test_sin_tema_avisa_y_no_envia(tmp_path: Path) -> None:
    result = _run(*_paths(tmp_path))

    assert result.returncode == 1
    assert "faltan datos" in result.stdout
    assert "tema" in result.stdout


def test_muestra_el_servidor_y_el_tema(tmp_path: Path) -> None:
    base = tmp_path / "alarm.toml"
    # Un puerto local cerrado falla al instante: la prueba no depende de la red.
    base.write_text(
        "\n".join(
            [
                "[notify.ntfy]",
                'server = "http://127.0.0.1:9"',
                'topic = "casa-abc123"',
                "timeout_s = 1",
                "retries = 0",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # El envio falla, pero la cabecera ya dice a donde iba.
    result = _run(*_paths(tmp_path))

    assert result.returncode == 1
    assert "http://127.0.0.1:9" in result.stdout
    assert "casa-abc123" in result.stdout
    assert "enviando" in result.stdout


def test_un_tema_invalido_falla_con_mensaje_claro(tmp_path: Path) -> None:
    base = tmp_path / "alarm.toml"
    base.write_text('[notify.ntfy]\ntopic = "con espacio"\n', encoding="utf-8")

    result = _run(*_paths(tmp_path))

    assert result.returncode == 1
    assert "notify.ntfy.topic" in result.stdout
