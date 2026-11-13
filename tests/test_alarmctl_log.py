"""Pruebas de `alarmctl log`, el subcomando que consulta el historial.

Se lanza como proceso aparte a proposito: lo que hay que comprobar es que el
comando que escribe el usuario funcione (argumentos, codigo de salida y texto),
no una funcion interna. Cada prueba trabaja sobre una base temporal, nunca
sobre `data/alarm.db`, que es el historial real.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import pytest

from alarm.services.storage import EventStore

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "alarmctl.py"


@pytest.fixture()
def historial(tmp_path: Path) -> Path:
    """Base temporal con tres eventos reconocibles."""
    path = tmp_path / "alarm.db"
    store = EventStore(path)
    try:
        store.append("info", "Enlace iniciado", "COM3", ts=1_700_000_000.0)
        store.append("triggered", "Alarma disparada", "sirena 120 s", ts=1_700_000_300.0)
        store.append("rearmed", "Rearmada tras disparo", "rearme 1 de 3", ts=1_700_000_600.0)
    finally:
        store.close()
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TOOL), "log", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        timeout=60,
    )


def test_lista_las_filas_del_historial(historial: Path) -> None:
    result = _run("--db", str(historial))
    assert result.returncode == 0, result.stderr
    assert "mostradas: 3 de 3" in result.stdout
    assert "Alarma disparada" in result.stdout
    assert "sirena 120 s" in result.stdout


def test_filtra_por_etiqueta_y_dice_cuantas_quedan(historial: Path) -> None:
    result = _run("--db", str(historial), "--tag", "triggered")
    assert result.returncode == 0, result.stderr
    assert "Alarma disparada" in result.stdout
    assert "Rearmada" not in result.stdout
    assert "mostradas: 1 de 1" in result.stdout


def test_cuenta_sin_listar(historial: Path) -> None:
    result = _run("--db", str(historial), "--tag", "rearmed", "--count")
    assert result.returncode == 0, result.stderr
    assert "1 filas" in result.stdout
    assert "Rearmada" not in result.stdout


def test_exporta_a_csv_solo_lo_filtrado(historial: Path, tmp_path: Path) -> None:
    destino = tmp_path / "disparos.csv"
    result = _run("--db", str(historial), "--tag", "triggered", "--csv", str(destino))
    assert result.returncode == 0, result.stderr
    assert "1 filas exportadas" in result.stdout

    with destino.open(encoding="utf-8-sig", newline="") as handle:
        filas = list(csv.reader(handle))
    assert len(filas) == 2, "cabecera y una fila"
    assert filas[1][3] == "Alarma disparada"


def test_la_espera_relativa_no_choca_con_argparse(historial: Path) -> None:
    """`--since -24h` es lo que se teclea; argparse lo confundiria con una opcion."""
    result = _run("--db", str(historial), "--since", "-24h")
    assert result.returncode == 0, result.stderr
    assert "no es una fecha válida" not in result.stdout


def test_una_fecha_mal_escrita_se_explica(historial: Path) -> None:
    result = _run("--db", str(historial), "--since", "ayer por la tarde")
    assert result.returncode == 1
    assert "no es una fecha válida" in result.stdout
    assert "2026-11-12" in result.stdout, "la ayuda dice qué formatos valen"


def test_un_historial_que_no_existe_no_crea_nada(tmp_path: Path) -> None:
    destino = tmp_path / "no-existe.db"
    result = _run("--db", str(destino))
    assert result.returncode == 1
    assert "no hay historial" in result.stdout
    assert not destino.exists(), "consultar nunca debe crear una base vacía"


def test_lista_las_etiquetas_guardadas(historial: Path) -> None:
    result = _run("--db", str(historial), "--tags")
    assert result.returncode == 0, result.stderr
    assert "triggered" in result.stdout
    assert "rearmed" in result.stdout
    assert "filas" in result.stdout
