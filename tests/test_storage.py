"""Pruebas del historial persistente en SQLite.

Lo que importa no es que SQLite funcione, sino que:

- lo escrito sobreviva a cerrar y reabrir la aplicacion;
- la retencion borre por antiguedad y por numero, y sea idempotente;
- el CSV que se exporta se abra bien en Excel y no se rompa con comas,
  comillas ni saltos de linea en el detalle.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from alarm.services.storage import CSV_HEADER, EventStore


def _abrir(path: Path, **kwargs: object) -> EventStore:
    return EventStore(path, **kwargs)  # type: ignore[arg-type]


def test_lo_escrito_sobrevive_a_cerrar_y_reabrir(tmp_path: Path) -> None:
    path = tmp_path / "alarm.db"
    store = _abrir(path)
    store.append("info", "Dispositivo", "Arduino Mega 2560")
    store.append("error", "Sin sensor", "Arduino desconectado")
    store.close()

    reabierto = _abrir(path)
    try:
        assert reabierto.count() == 2
        rows = reabierto.recent(10)
        assert [row.kind for row in rows] == ["Dispositivo", "Sin sensor"]
        assert rows[1].tag == "error"
        assert rows[1].detail == "Arduino desconectado"
    finally:
        reabierto.close()


def test_recent_devuelve_las_ultimas_en_orden_cronologico(tmp_path: Path) -> None:
    store = _abrir(tmp_path / "alarm.db", max_events=100)
    try:
        for index in range(5):
            store.append("info", f"evento {index}", "detalle")
        rows = store.recent(3)
        assert [row.kind for row in rows] == ["evento 2", "evento 3", "evento 4"]
        assert store.recent(0) == []
    finally:
        store.close()


def test_purga_por_antiguedad(tmp_path: Path) -> None:
    ahora = 1_700_000_000.0
    store = _abrir(tmp_path / "alarm.db", max_age_days=365, max_events=1_000, clock=lambda: ahora)
    try:
        store.append("info", "viejo", "hace dos anos", ts=ahora - 2 * 365 * 86_400)
        store.append("info", "reciente", "ayer", ts=ahora - 86_400)
        assert store.count() == 2

        removed = store.purge()
        assert removed == 1
        assert [row.kind for row in store.recent(10)] == ["reciente"]
        # Repetir no borra nada mas: la retencion es idempotente.
        assert store.purge() == 0
    finally:
        store.close()


def test_purga_por_numero(tmp_path: Path) -> None:
    store = _abrir(tmp_path / "alarm.db", max_age_days=3650, max_events=4)
    try:
        for index in range(10):
            store.append("info", f"evento {index}", "detalle", ts=1_700_000_000.0 + index)
        removed = store.purge()
        assert removed == 6
        assert store.count() == 4
        assert [row.kind for row in store.recent(10)] == [
            "evento 6",
            "evento 7",
            "evento 8",
            "evento 9",
        ]
    finally:
        store.close()


def test_el_tope_se_aplica_al_insertar(tmp_path: Path) -> None:
    store = _abrir(tmp_path / "alarm.db", max_age_days=3650, max_events=3)
    try:
        # La comprobacion corre cada 500 inserciones; 500 con tope 3 deja 3.
        for index in range(500):
            store.append("info", f"evento {index}", "detalle", ts=1_700_000_000.0 + index)
        assert store.count() == 3
    finally:
        store.close()


def test_exporta_csv_con_bom_encabezado_y_escapes(tmp_path: Path) -> None:
    store = _abrir(tmp_path / "alarm.db")
    try:
        store.append("info", "Dispositivo", "Arduino Mega 2560", ts=1_700_000_000.0)
        store.append(
            "error",
            "Aviso, con coma",
            'Detalle con "comillas" y\nsalto de línea',
            ts=1_700_000_060.0,
        )
        destino = tmp_path / "registro.csv"
        assert store.export_csv(destino) == 2
    finally:
        store.close()

    raw = destino.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "Excel necesita el BOM para los acentos"

    with destino.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert tuple(rows[0]) == CSV_HEADER
    assert len(rows) == 3
    assert rows[1][2] == "info"
    assert rows[1][3] == "Dispositivo"
    assert rows[2][3] == "Aviso, con coma"
    assert "salto de línea" in rows[2][4]


def test_exporta_solo_cuando_hay_filas(tmp_path: Path) -> None:
    store = _abrir(tmp_path / "vacio.db")
    try:
        destino = tmp_path / "vacio.csv"
        assert store.export_csv(destino) == 0
    finally:
        store.close()
    lines = destino.read_text(encoding="utf-8-sig").splitlines()
    assert lines == [",".join(CSV_HEADER)]


def test_en_memoria_funciona_y_no_toca_el_disco() -> None:
    store = EventStore(":memory:")
    try:
        store.append("info", "Prueba", "en memoria")
        assert store.count() == 1
        assert store.recent(1)[0].kind == "Prueba"
    finally:
        store.close()


def test_clear_vacia_el_historial(tmp_path: Path) -> None:
    store = _abrir(tmp_path / "alarm.db")
    try:
        store.append("info", "Uno", "")
        store.append("info", "Dos", "")
        store.clear()
        assert store.count() == 0
        assert store.recent(10) == []
    finally:
        store.close()


def test_cerrar_dos_veces_no_falla(tmp_path: Path) -> None:
    store = _abrir(tmp_path / "alarm.db")
    store.close()
    store.close()


# ---------------------------------------------------------------------------
# Estado del sistema (armado persistente y marcas de tiempo)
# ---------------------------------------------------------------------------


def test_el_estado_guardado_sobrevive_a_reabrir(tmp_path: Path) -> None:
    path = tmp_path / "alarm.db"
    store = _abrir(path)
    try:
        assert store.get_state("armed_intent") is None
        assert store.get_state("armed_intent", "false") == "false"
        store.set_state("armed_intent", "true")
        store.set_state("armed_at", "1700000000")
    finally:
        store.close()

    reabierto = _abrir(path)
    try:
        assert reabierto.get_state("armed_intent") == "true"
        assert reabierto.get_state("armed_at") == "1700000000"
    finally:
        reabierto.close()


def test_set_state_actualiza_en_sitio(tmp_path: Path) -> None:
    store = _abrir(tmp_path / "alarm.db")
    try:
        store.set_state("armed_intent", "true")
        store.set_state("armed_intent", "false")
        assert store.get_state("armed_intent") == "false"
        store.clear_state("armed_intent")
        assert store.get_state("armed_intent") is None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Consulta de solo lectura (la que usa `alarmctl log`)
# ---------------------------------------------------------------------------


def _historial(tmp_path: Path) -> Path:
    """Historial con tres filas de fechas y etiquetas conocidas."""
    path = tmp_path / "alarm.db"
    store = _abrir(path)
    try:
        store.append("info", "Enlace iniciado", "COM3", ts=1_700_000_000.0)
        store.append("triggered", "Alarma disparada", "sirena 120 s", ts=1_700_000_300.0)
        store.append("rearmed", "Rearmada tras disparo", "rearme 1 de 3", ts=1_700_000_600.0)
    finally:
        store.close()
    return path


def test_consulta_filtra_por_etiqueta_y_texto(tmp_path: Path) -> None:
    from alarm.services.storage import query_rows

    path = _historial(tmp_path)
    assert [row.kind for row in query_rows(path)] == [
        "Enlace iniciado",
        "Alarma disparada",
        "Rearmada tras disparo",
    ]
    assert [row.tag for row in query_rows(path, tag="triggered")] == ["triggered"]
    # El texto se busca por subcadena y sin distinguir mayusculas.
    assert [row.tag for row in query_rows(path, kind="REARM")] == ["rearmed"]
    assert [row.tag for row in query_rows(path, text="SIRENA")] == ["triggered"]
    assert [row.tag for row in query_rows(path, text="rearme 1")] == ["rearmed"]


def test_consulta_filtra_por_rango_de_fechas(tmp_path: Path) -> None:
    from alarm.services.storage import query_rows

    path = _historial(tmp_path)
    rows = query_rows(path, since=1_700_000_100.0, until=1_700_000_500.0)
    assert [row.kind for row in rows] == ["Alarma disparada"]


def test_consulta_limita_a_las_mas_recientes_sin_desordenar(tmp_path: Path) -> None:
    from alarm.services.storage import query_rows

    path = _historial(tmp_path)
    rows = query_rows(path, limit=2)
    assert [row.kind for row in rows] == ["Alarma disparada", "Rearmada tras disparo"]
    assert len(query_rows(path, limit=0)) == 3, "0 significa todas"


def test_contar_y_etiquetas(tmp_path: Path) -> None:
    from alarm.services.storage import count_rows, tag_counts

    path = _historial(tmp_path)
    assert count_rows(path) == 3
    assert count_rows(path, tag="info") == 1
    assert count_rows(path, since=1_700_000_400.0) == 1
    assert tag_counts(path) == [("info", 1), ("rearmed", 1), ("triggered", 1)]


def test_la_consulta_no_modifica_el_fichero(tmp_path: Path) -> None:
    from alarm.services.storage import query_rows

    path = _historial(tmp_path)
    antes = path.read_bytes()
    query_rows(path, limit=1)
    assert path.read_bytes() == antes, "consultar no reescribe ni crea nada"


def test_consultar_un_historial_inexistente_lo_dice(tmp_path: Path) -> None:
    from alarm.services.storage import query_rows

    with pytest.raises(FileNotFoundError):
        query_rows(tmp_path / "no-existe.db")


def test_escribir_csv_suelta_las_filas_que_le_dan(tmp_path: Path) -> None:
    from alarm.services.storage import query_rows, write_csv

    path = _historial(tmp_path)
    destino = tmp_path / "solo-disparos.csv"
    rows = query_rows(path, tag="triggered")
    assert write_csv(destino, rows) == 1

    with destino.open(encoding="utf-8-sig", newline="") as handle:
        leidas = list(csv.reader(handle))
    assert tuple(leidas[0]) == CSV_HEADER
    assert leidas[1][2] == "triggered"
    assert leidas[1][3] == "Alarma disparada"
    assert write_csv(tmp_path / "vacio.csv", []) == 0
