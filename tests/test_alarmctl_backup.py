"""Pruebas de `alarmctl backup`, el subcomando de copia completa.

Se lanza como proceso aparte, igual que `log`: lo que importa es que la orden
que teclea el usuario funcione (argumentos, codigo de salida y avisos) y que no
toque la configuracion real del repositorio. Todas las rutas van a `tmp_path`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "alarmctl.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "backup", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
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


def test_export_sin_secretos_escribe_el_fichero(tmp_path: Path) -> None:
    destino = tmp_path / "copia.alarmbackup"

    result = _run("export", "--out", str(destino), "--no-secrets", *_paths(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "secretos: ninguno" in result.stdout
    data = json.loads(destino.read_text(encoding="utf-8"))
    assert data["formato"] == "alarma-casa-copia"
    assert data["secretos"] == {}
    assert "serial" in data["config"]


def test_export_anade_la_extension(tmp_path: Path) -> None:
    result = _run(
        "export", "--out", str(tmp_path / "sin-extension"), "--no-secrets", *_paths(tmp_path)
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "sin-extension.alarmbackup").is_file()


def test_export_avisa_de_que_lleva_credenciales(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ALARM_SMTP_PASSWORD=secreto-real\n", encoding="utf-8")

    result = _run("export", "--out", str(tmp_path / "copia.alarmbackup"), *_paths(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "ALARM_SMTP_PASSWORD" in result.stdout
    assert "credenciales en claro" in result.stdout


def test_import_escribe_la_configuracion_local(tmp_path: Path) -> None:
    copia = tmp_path / "copia.alarmbackup"
    assert _run("export", "--out", str(copia), "--no-secrets", *_paths(tmp_path)).returncode == 0

    result = _run("import", "--from", str(copia), "--yes", *_paths(tmp_path))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "alarm.local.toml").is_file()
    assert "configuración escrita" in result.stdout


def test_import_sin_confirmacion_no_toca_nada(tmp_path: Path) -> None:
    """Sin `--yes` y sin terminal interactiva, la respuesta es no."""
    copia = tmp_path / "copia.alarmbackup"
    assert _run("export", "--out", str(copia), "--no-secrets", *_paths(tmp_path)).returncode == 0

    result = _run("import", "--from", str(copia), *_paths(tmp_path))

    assert result.returncode == 1
    assert "cancelado" in result.stdout
    assert not (tmp_path / "alarm.local.toml").exists()


def test_import_de_un_fichero_ajeno_falla(tmp_path: Path) -> None:
    otro = tmp_path / "cualquiera.json"
    otro.write_text('{"hola": 1}', encoding="utf-8")

    result = _run("import", "--from", str(otro), "--yes", *_paths(tmp_path))

    assert result.returncode == 1
    assert "no es una copia completa" in result.stdout
