"""Pruebas del servicio de voz sin reproducir audio real."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

from alarm.config import default_config
from alarm.services.tts import SpeechService, build_sapi_script, describe_voice


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeSynthesizer:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def __call__(self, text: str, path: Path) -> None:
        self.texts.append(text)
        path.write_bytes(b"RIFF" + b"\x00" * 32)


class FakePlayer:
    def __init__(self, *, block: threading.Event | None = None) -> None:
        self.played: list[Path] = []
        self.stops = 0
        self._block = block

    def play(self, path: Path) -> None:
        self.played.append(path)
        if self._block is not None:
            self._block.wait(2.0)

    def stop(self) -> None:
        self.stops += 1


def _service(tmp_path: Path) -> SpeechService:
    config = default_config().tts
    return SpeechService(
        config,
        cache_dir=tmp_path,
        synthesizer=FakeSynthesizer(),
        player=FakePlayer(),
    )


def test_el_guion_escapa_comillas() -> None:
    script = build_sapi_script(
        "No te muevas: es la voz de O'Brien", Path("c:/tmp/voz.wav"), voice="", rate=0, volume=90
    )
    assert "O''Brien" in script
    assert "SetOutputToWaveFile('c:\\tmp\\voz.wav')" in script or "voz.wav" in script
    assert "SelectVoice" not in script


def test_el_guion_selecciona_la_voz() -> None:
    script = build_sapi_script("hola", Path("x.wav"), voice="Microsoft Helena", rate=2, volume=50)
    assert "SelectVoice('Microsoft Helena')" in script
    assert "$s.Rate = 2" in script
    assert "$s.Volume = 50" in script


def test_sin_voz_no_hace_nada(tmp_path: Path) -> None:
    config = replace(default_config().tts, enabled=False)
    service = SpeechService(
        config, cache_dir=tmp_path, synthesizer=FakeSynthesizer(), player=FakePlayer()
    )
    try:
        assert service.say("hola") is False
    finally:
        service.close()


def test_reproduce_y_cachea(tmp_path: Path) -> None:
    synth = FakeSynthesizer()
    player = FakePlayer()
    config = replace(default_config().tts, repeat=2)
    service = SpeechService(config, cache_dir=tmp_path, synthesizer=synth, player=player)
    try:
        assert service.say("alarma armada") is True
        assert _wait(lambda: service.spoken == 1)
        assert len(player.played) == 2  # repeat
        assert synth.texts == ["alarma armada"]
        assert player.played[0].parent == tmp_path
        assert player.played[0].suffix == ".wav"

        # El segundo aviso usa la cache: no se vuelve a sintetizar.
        assert service.say("alarma armada") is True
        assert _wait(lambda: service.spoken == 2)
        assert synth.texts == ["alarma armada"]
    finally:
        service.close()


def test_texto_vacio_no_se_encola(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        assert service.say("   ") is False
    finally:
        service.close()


def test_cola_acotada_descarta_lo_mas_antiguo(tmp_path: Path) -> None:
    block = threading.Event()
    player = FakePlayer(block=block)
    config = default_config().tts
    service = SpeechService(
        config,
        cache_dir=tmp_path,
        synthesizer=FakeSynthesizer(),
        player=player,
        max_queue=1,
    )
    try:
        assert service.say("uno") is True
        assert _wait(lambda: len(player.played) == 1)  # el reproductor esta bloqueado
        assert service.say("dos") is True
        assert service.say("tres") is True
        assert service.dropped == 1
    finally:
        block.set()
        service.close()


def test_stop_vacia_la_cola(tmp_path: Path) -> None:
    player = FakePlayer()
    service = SpeechService(
        default_config().tts,
        cache_dir=tmp_path,
        synthesizer=FakeSynthesizer(),
        player=player,
    )
    try:
        assert service.say("uno") is True
        service.stop()
        assert player.stops >= 1
    finally:
        service.close()


def _service_con(tmp_path: Path, **eventos: object) -> tuple[SpeechService, FakePlayer]:
    base = default_config().tts
    events = dict(base.events)
    for key, cambios in eventos.items():
        events[key] = replace(events[key], **cambios)  # type: ignore[arg-type]
    config = replace(base, events=events)
    player = FakePlayer()
    service = SpeechService(
        config, cache_dir=tmp_path, synthesizer=FakeSynthesizer(), player=player
    )
    return service, player


def test_cada_evento_usa_sus_repeticiones(tmp_path: Path) -> None:
    service, player = _service_con(tmp_path, armed={"repeat": 3})
    try:
        assert service.say_event("armed", "alarma armada") is True
        assert _wait(lambda: service.spoken == 1)
        assert len(player.played) == 3
    finally:
        service.close()


def test_las_repeticiones_se_pueden_forzar_desde_fuera(tmp_path: Path) -> None:
    service, player = _service_con(tmp_path, armed={"repeat": 1})
    try:
        assert service.say_event("armed", "alarma armada", repeat=4) is True
        assert _wait(lambda: service.spoken == 1)
        assert len(player.played) == 4
    finally:
        service.close()


def test_el_intervalo_separa_las_repeticiones(tmp_path: Path) -> None:
    service, player = _service_con(tmp_path, armed={"repeat": 3, "gap_ms": 150})
    try:
        assert service.say_event("armed", "alarma armada") is True
        assert _wait(lambda: len(player.played) >= 1, timeout=1.0)
        assert len(player.played) < 3, "con intervalo no caben las tres de golpe"
        assert _wait(lambda: service.spoken == 1, timeout=3.0)
        assert len(player.played) == 3
    finally:
        service.close()


def test_cancelar_durante_el_intervalo_corta_las_repeticiones(tmp_path: Path) -> None:
    service, player = _service_con(tmp_path, armed={"repeat": 5, "gap_ms": 400})
    try:
        assert service.say_event("armed", "alarma armada") is True
        assert _wait(lambda: len(player.played) == 1, timeout=1.0)
        assert service.cancel_pending("armed") == 1
        time.sleep(0.6)
        assert len(player.played) == 1, "el intervalo cancelado no debe repetir"
    finally:
        service.close()


def test_un_evento_desactivado_no_suena(tmp_path: Path) -> None:
    service, player = _service_con(tmp_path, armed={"enabled": False})
    try:
        assert service.say_event("armed", "alarma armada") is False
        assert service.pending == 0
        assert player.played == []
    finally:
        service.close()


def test_el_retardo_retrasa_el_aviso(tmp_path: Path) -> None:
    service, player = _service_con(tmp_path, disarmed={"delay_ms": 250})
    try:
        assert service.say_event("disarmed", "alarma desarmada") is True
        time.sleep(0.05)
        assert player.played == []
        assert _wait(lambda: service.spoken == 1, timeout=3.0)
        assert player.played
    finally:
        service.close()


def test_cancelar_lo_programado_evita_que_suene(tmp_path: Path) -> None:
    service, player = _service_con(tmp_path, triggered={"delay_ms": 400})
    try:
        assert service.say_event("triggered", "intruso") is True
        assert service.cancel_pending() == 1
        assert service.pending == 0
        assert _wait(lambda: player.played, timeout=0.7) is False
    finally:
        service.close()


def test_cancelar_por_clave_no_toca_los_demas(tmp_path: Path) -> None:
    service, _ = _service_con(
        tmp_path,
        armed={"delay_ms": 400},
        disarmed={"delay_ms": 400},
    )
    try:
        assert service.say_event("armed", "alarma armada") is True
        assert service.say_event("disarmed", "alarma desarmada") is True
        assert service.cancel_pending("armed") == 1
        assert service.pending == 1
    finally:
        service.close()


def test_cerrar_cancela_lo_pendiente(tmp_path: Path) -> None:
    service, _ = _service_con(tmp_path, armed={"delay_ms": 1000})
    assert service.say_event("armed", "alarma armada") is True
    service.close()
    assert service.pending == 0


def test_describir_el_aviso_para_tooltips() -> None:
    config = default_config().tts
    texto = describe_voice(config, "triggered")
    assert "texto=" in texto
    assert "repeticiones=" in texto
    assert "presencia" in texto.lower()

    con_intervalo = replace(
        config,
        events={
            **config.events,
            "triggered": replace(config.event("triggered"), gap_ms=1500, log=False),
        },
    )
    detalle = describe_voice(con_intervalo, "triggered")
    assert "intervalo=1.5 s" in detalle
    assert "sin registro" in detalle
