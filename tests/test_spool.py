"""Pruebas del buzon durable de eventos.

El buzon es la segunda linea de defensa del ACK: si SQLite falla, el evento
tiene que quedar en un fichero sincronizado antes de confirmarlo. Aqui se
comprueba que escribe de verdad, que reimporta sin duplicar y que un fichero
corrupto se aparta en vez de perderse.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from alarm.services.spool import EventSpool, SpoolRecord
from alarm.services.storage import EventStore


def _registro(seq: int = 1, boot_id: int | None = 3, **kwargs: object) -> SpoolRecord:
    values: dict[str, object] = {
        "boot_id": boot_id,
        "seq": seq,
        "tag": "start",
        "kind": "Movimiento",
        "detail": "Detección iniciada",
        "ts": 1_700_000_000.0,
    }
    values.update(kwargs)
    return SpoolRecord(**values)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "alarm.db")


def test_append_escribe_una_linea_json(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path / "spool")
    assert spool.append(_registro()) is True

    files = spool.files()
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["seq"] == 1
    assert data["boot_id"] == 3
    assert data["detail"] == "Detección iniciada"
    assert spool.pending == 1


def test_varios_registros_van_al_mismo_fichero_del_dia(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path / "spool")
    for seq in (1, 2, 3):
        assert spool.append(_registro(seq=seq))
    assert len(spool.files()) == 1
    assert spool.pending == 3


def test_el_buzon_sobrevive_a_un_proceso_nuevo(tmp_path: Path) -> None:
    EventSpool(tmp_path / "spool").append(_registro())

    otro = EventSpool(tmp_path / "spool")
    assert otro.refresh() == 1


def test_drain_importa_y_borra_el_fichero(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path / "spool")
    spool.append(_registro(seq=1))
    spool.append(_registro(seq=2))
    store = _store(tmp_path)
    try:
        assert spool.drain_into(store) == 2
        assert store.count() == 2
        assert store.has_processed(3, 1)
        assert store.has_processed(3, 2)
        assert spool.files() == []
        assert spool.pending == 0
    finally:
        store.close()


def test_drain_es_idempotente(tmp_path: Path) -> None:
    """Reimportar dos veces no duplica: la clave (boot_id, seq) manda."""
    store = _store(tmp_path)
    spool = EventSpool(tmp_path / "spool")
    try:
        spool.append(_registro(seq=1))
        assert spool.drain_into(store) == 1
        # Se vuelve a dejar el mismo registro, como si el borrado fallara.
        spool.append(_registro(seq=1))
        assert spool.drain_into(store) == 0
        assert store.count() == 1
    finally:
        store.close()


def test_drain_restaura_el_estado_guardado_con_el_evento(tmp_path: Path) -> None:
    store = _store(tmp_path)
    spool = EventSpool(tmp_path / "spool")
    try:
        spool.append(
            _registro(state={"armed_intent": "true", "alarm_snapshot": '{"state":"triggered"}'})
        )
        assert spool.drain_into(store) == 1
        assert store.get_state("armed_intent") == "true"
        assert store.get_state("alarm_snapshot") == '{"state":"triggered"}'
    finally:
        store.close()


def test_un_fichero_corrupto_se_aparta(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path / "spool")
    spool.directory.mkdir(parents=True, exist_ok=True)
    roto = spool.directory / "events-20260101.jsonl"
    roto.write_text("esto no es json\n", encoding="utf-8")
    store = _store(tmp_path)
    try:
        assert spool.drain_into(store) == 0
        assert not roto.exists()
        assert (spool.directory / "events-20260101.jsonl.bad").exists()
        assert store.count() == 0
    finally:
        store.close()


def test_append_devuelve_false_si_no_puede_escribir(tmp_path: Path) -> None:
    """Sin buzon y sin base no hay nada durable: el ACK tendra que esperar."""
    bloqueado = tmp_path / "no-es-carpeta"
    bloqueado.write_text("x", encoding="utf-8")
    spool = EventSpool(bloqueado / "spool")

    assert spool.append(_registro()) is False
    assert spool.pending == 0


def test_un_registro_incompleto_no_se_reimporta(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path / "spool")
    spool.directory.mkdir(parents=True, exist_ok=True)
    (spool.directory / "events-20260101.jsonl").write_text('{"seq": 1}\n', encoding="utf-8")
    store = _store(tmp_path)
    try:
        assert spool.drain_into(store) == 0
        assert store.count() == 0
        assert (spool.directory / "events-20260101.jsonl.bad").exists()
    finally:
        store.close()


def test_las_lineas_buenas_se_importan_aunque_haya_una_rota(tmp_path: Path) -> None:
    """Una linea corrupta no puede arrastrar consigo los eventos validos.

    Antes, un solo byte malo apartaba el fichero entero y los eventos buenos se
    quedaban fuera del historial: la cuarentena protegia la evidencia a costa de
    perder justo lo que habia que salvar.
    """
    spool = EventSpool(tmp_path / "spool")
    spool.directory.mkdir(parents=True, exist_ok=True)
    roto = spool.directory / "events-20260101.jsonl"
    bueno_1 = json.dumps(_registro(seq=1).as_dict())
    bueno_2 = json.dumps(_registro(seq=2).as_dict())
    roto.write_text(f"{bueno_1}\nbasura a medias\n{bueno_2}\n", encoding="utf-8")

    store = _store(tmp_path)
    try:
        assert spool.drain_into(store) == 2
        assert store.count() == 2
        assert store.has_processed(3, 1) and store.has_processed(3, 2)
        assert not roto.exists()
        apartado = spool.directory / "events-20260101.jsonl.bad"
        assert apartado.read_text(encoding="utf-8").strip() == "basura a medias"
    finally:
        store.close()


def test_una_linea_a_medias_no_se_come_el_registro_siguiente(tmp_path: Path) -> None:
    """Un apagon a mitad de escritura deja el fichero sin salto final.

    El siguiente ``append`` no puede pegarse a ese fragmento: si lo hiciera,
    el registro nuevo quedaria dentro de una linea invalida y se perderia.
    """
    momento = 1_700_000_000.0
    dia = time.strftime("%Y%m%d", time.localtime(momento))
    spool = EventSpool(tmp_path / "spool", clock=lambda: momento)
    spool.directory.mkdir(parents=True, exist_ok=True)
    path = spool.directory / f"events-{dia}.jsonl"
    path.write_text('{"boot_id": 3, "seq": 1, "ta', encoding="utf-8")

    assert spool.append(_registro(seq=2)) is True

    lineas = path.read_text(encoding="utf-8").splitlines()
    assert len(lineas) == 2, "el fragmento y el registro nuevo van en lineas distintas"
    assert json.loads(lineas[1])["seq"] == 2

    store = _store(tmp_path)
    try:
        # El fragmento roto se aparta y el registro bueno entra.
        assert spool.drain_into(store) == 1
        assert store.has_processed(3, 2)
        apartado = spool.directory / f"events-{dia}.jsonl.bad"
        assert '{"boot_id": 3, "seq": 1, "ta' in apartado.read_text(encoding="utf-8")
    finally:
        store.close()


def test_la_cuarentena_se_acumula_sin_pisar_la_anterior(tmp_path: Path) -> None:
    spool = EventSpool(tmp_path / "spool")
    spool.directory.mkdir(parents=True, exist_ok=True)
    path = spool.directory / "events-20260101.jsonl"
    store = _store(tmp_path)
    try:
        path.write_text("primera rota\n", encoding="utf-8")
        assert spool.drain_into(store) == 0
        path.write_text("segunda rota\n", encoding="utf-8")
        assert spool.drain_into(store) == 0

        texto = (spool.directory / "events-20260101.jsonl.bad").read_text(encoding="utf-8")
        assert "primera rota" in texto and "segunda rota" in texto
    finally:
        store.close()


def test_el_registro_va_y_vuelve_por_json() -> None:
    original = _registro(
        seq=9,
        state={"armed_intent": "false"},
        remove_state=("armed_at",),
    )

    vuelta = SpoolRecord.from_dict(json.loads(json.dumps(original.as_dict())))

    assert vuelta is not None
    assert vuelta.boot_id == original.boot_id
    assert vuelta.seq == original.seq
    assert vuelta.state == original.state
    assert vuelta.remove_state == original.remove_state


def test_sin_boot_id_el_registro_tambien_viaja() -> None:
    original = _registro(boot_id=None)
    vuelta = SpoolRecord.from_dict(original.as_dict())

    assert vuelta is not None
    assert vuelta.boot_id is None
