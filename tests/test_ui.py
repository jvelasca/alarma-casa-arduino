"""Pruebas de la interfaz Qt.

No comprueban pixeles: comprueban que la estructura existe (menus, barra,
registro), que el estado se pinta donde debe y que la cadena completa
dispositivo -> puente -> ventana llega hasta el registro.

El plugin `offscreen` de Qt (ver `conftest.py`) permite construir la ventana sin
pantalla. La voz y el notificador se sustituyen por dobles: una prueba no debe
sonar ni abrir conexiones SMTP.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import replace

import pytest

from alarm.config import AppConfig, default_config
from alarm.device.manager import DeviceManager
from alarm.services.base import DeliveryResult
from alarm.transport.simulated_link import SimulatedLink


class FakeSpeech:
    """Sustituye al servicio de voz: registra lo que se habria dicho."""

    def __init__(self) -> None:
        self.enabled = True
        self.spoken: list[str] = []
        self.pending: list[str] = []
        self.stopped = 0

    def say_event(
        self, key: str, text: str, *, repeat: int | None = None, delay_ms: int | None = None
    ) -> bool:
        self.spoken.append(text)
        self.pending.append(key)
        return True

    def say(self, text: str) -> bool:
        return self.say_event("generic", text)

    def cancel_pending(self, keys: object = None) -> int:
        return 0

    def stop(self) -> None:
        self.stopped += 1

    def close(self) -> None:
        pass

    def set_enabled(self, value: bool) -> None:
        self.enabled = bool(value)

    def reconfigure(self, _config: object) -> None:
        pass


class FakeNotifier:
    """Notificador que apunta lo que se le pide, sin hilos ni red."""

    def __init__(self, channels: list[str] | None = None) -> None:
        self.channel_names = channels if channels is not None else ["email"]
        self.submitted: list[tuple[str, str, str]] = []

    @property
    def active(self) -> bool:
        return bool(self.channel_names)

    def channels_for(self, _cause: str) -> list[str]:
        return list(self.channel_names)

    def submit(self, cause: str, subject: str, body: str, *, ignore_gate: bool = False) -> bool:
        self.submitted.append((cause, subject, body))
        return True

    def reconfigure(self, _channels: object) -> None:
        pass

    def close(self) -> None:
        pass


def _config(**kwargs: object) -> AppConfig:
    base = default_config()
    return replace(base, **kwargs)  # type: ignore[arg-type]


def _config_con_correo(*, on_armed: bool = True) -> AppConfig:
    base = default_config()
    email = replace(
        base.email,
        enabled=True,
        on_armed=on_armed,
        host="smtp.example.com",
        sender="alarma@example.com",
        to=("yo@example.com",),
        user="alarma@example.com",
        password="secreto",
    )
    # Sin retardo de salida: estas pruebas esperan que armar avise ya.
    return replace(
        base,
        email=email,
        alarm=replace(base.alarm, exit_delay_s=0, entry_delay_s=0),
    )


@pytest.fixture(autouse=True)
def sin_cuadros_modales(monkeypatch):
    """Neutraliza los `QMessageBox` modales.

    En offscreen un `QMessageBox.warning` sigue siendo modal: bloquearia la
    prueba esperando un clic que nunca llega. Las pruebas comprueban la logica
    de validacion, no el dialogo de aviso.

    `question` responde `Yes` porque hay acciones que solo siguen si el usuario
    confirma; quien pruebe el caso contrario lo parchea a mano.
    """
    from PyQt6.QtWidgets import QMessageBox

    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(
            QMessageBox,
            name,
            staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Ok),
        )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )


@pytest.fixture()
def window(qapp: object, tmp_path) -> Iterator[object]:
    """Ventana con voz y notificador dobles y rutas de configuracion temporales."""
    from alarm.ui.app import MainWindow

    instance = MainWindow(
        simulated=True,
        autoconnect=False,
        # Sin retardos: estas pruebas comprueban el resto de la ventana y no
        # pueden esperar 45 s a que la alarma vigile. Los retardos tienen sus
        # propias pruebas con un reloj falso.
        config=replace(
            default_config(),
            alarm=replace(default_config().alarm, exit_delay_s=0, entry_delay_s=0),
        ),
        config_path=tmp_path / "alarm.toml",
        local_path=tmp_path / "alarm.local.toml",
        env_path=tmp_path / ".env",
        history_path=tmp_path / "alarm.db",
    )
    instance._speech = FakeSpeech()  # type: ignore[assignment]
    instance._notifier = FakeNotifier()  # type: ignore[assignment]
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.deleteLater()


def _menu_titles(window: object) -> list[str]:
    menubar = window.menuBar()  # type: ignore[attr-defined]
    assert menubar is not None
    return [action.text().replace("&", "") for action in menubar.actions()]


def _menu(window: object, title: str):
    menubar = window.menuBar()  # type: ignore[attr-defined]
    assert menubar is not None
    for action in menubar.actions():
        if action.text().replace("&", "") == title:
            return action.menu()
    raise AssertionError(f"no existe el menu {title!r}")


def _labels(menu) -> list[str]:
    return [action.text() for action in menu.actions() if not action.isSeparator()]


# ---------------------------------------------------------------------------
# Estructura
# ---------------------------------------------------------------------------


def test_barra_de_menus_tiene_ajuste_sensor(window) -> None:
    assert _menu_titles(window) == [
        "Archivo",
        "Alarma",
        "Herramientas",
        "Avisos",
        "Ver",
        "Ayuda",
    ]

    herramientas = _menu(window, "Herramientas")
    labels = _labels(herramientas)
    assert labels[0] == "Ajuste del sensor…"
    shortcut = herramientas.actions()[0].shortcut().toString()
    assert shortcut == "Ctrl+A"


def test_barra_de_herramientas_expone_lo_principal(window) -> None:
    texts = [
        action.text()
        for action in window._toolbar.actions()  # type: ignore[attr-defined]
        if not action.isSeparator() and action.text()
    ]
    for esperado in ("Armar", "Desarmar", "Silenciar", "Ajuste del sensor…", "Conectar"):
        assert esperado in texts


def test_panel_principal_se_construye(window) -> None:
    assert "Alarma" in window.windowTitle()  # type: ignore[attr-defined]
    assert window._log.topLevelItemCount() == 0  # type: ignore[attr-defined]
    assert window._events_label.text() == "0"  # type: ignore[attr-defined]
    assert window._primary.text() == "Armar"  # type: ignore[attr-defined]
    assert window._hero._state.text() == "DESARMADA"  # type: ignore[attr-defined]


def test_cadena_completa_dispositivo_puente_ventana(window, qapp) -> None:
    """Los eventos simulados deben acabar visibles en el registro."""
    link = SimulatedLink(warmup_s=0.3, speed=10.0, motion_every_s=(0.5, 0.9), hold_ms=2700)
    manager = DeviceManager(link, auto_ack=True, on_message=window._bridge.push_message)
    window._manager = manager
    manager.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            qapp.processEvents()  # type: ignore[attr-defined]
            if window._log.topLevelItemCount() >= 4:
                break
            time.sleep(0.02)

        rows = window._log.rows()
        assert len(rows) >= 4, f"no llegaron eventos (filas={len(rows)})"
        joined = " | ".join(" ".join(row) for row in rows)
        assert "Dispositivo" in joined
        assert "Movimiento" in joined
        assert int(window._pulses_label.text()) >= 1
    finally:
        window._manager = None
        manager.stop(timeout=0.5)


def test_omitir_calentamiento_sin_dispositivo_avisa(window, monkeypatch) -> None:
    from alarm.ui import app as app_module

    avisos: list[str] = []
    monkeypatch.setattr(
        app_module.QMessageBox,
        "warning",
        lambda *args, **kwargs: avisos.append(args[2] if len(args) > 2 else ""),
    )
    window.skip_warmup()
    assert avisos and "dispositivo" in avisos[0].lower()


def test_dialogo_de_ajuste_abre_y_cierra(window) -> None:
    from alarm.ui.bridge import QtBridge
    from alarm.ui.calibration import CalibrationDialog

    manager = DeviceManager(SimulatedLink(), auto_ack=True)
    bridge = QtBridge()
    cerrado: list[bool] = []
    dialog = CalibrationDialog(
        window, manager=manager, bridge=bridge, on_close=lambda: cerrado.append(True)
    )
    try:
        assert dialog.windowTitle().startswith("Ajuste del sensor")
        assert dialog._prep_s.value() >= 0
        assert dialog._quiet_s.value() > 0
        assert dialog._motion_s.value() > 0
        dialog.close()
        assert cerrado == [True]
    finally:
        dialog.close()
        manager.stop(timeout=0.5)


def test_el_ajuste_sigue_vivo_al_acabar_la_cuenta_atras(window, qapp) -> None:
    """Regresion: al terminar la preparacion la aplicacion se cerraba sola.

    `_start_prep()` cambia de paso y destruye los `QSpinBox` de la introduccion.
    Las duraciones se leian de esos widgets al acabar la cuenta atras, ya
    borrados: el `RuntimeError` salia de un slot de Qt y PyQt6 abortaba el
    proceso. Ahora se copian antes de destruirlos.
    """
    from alarm.ui.bridge import QtBridge
    from alarm.ui.calibration import CalibrationDialog

    manager = DeviceManager(SimulatedLink(), auto_ack=True)
    manager.start()
    bridge = QtBridge()
    dialog = CalibrationDialog(window, manager=manager, bridge=bridge)
    try:
        dialog._prep_s.setValue(5)
        dialog._quiet_s.setValue(20)
        dialog._motion_s.setValue(25)
        dialog._start_prep()
        # Deja que Qt ejecute los `deleteLater()` de la introduccion: es el
        # estado en el que arrancaba el fallo.
        qapp.processEvents()

        dialog._prep_remaining = 1
        dialog._prep_tick()  # antes: RuntimeError y cierre de la aplicacion

        assert dialog._phase_name == "quiet"
        assert dialog._phase_active
        assert dialog._durations == {"prep": 5, "quiet": 20, "motion": 25}
    finally:
        dialog.close()
        manager.stop(timeout=0.5)


def test_repetir_el_ajuste_conserva_las_duraciones(window, qapp) -> None:
    from alarm.ui.bridge import QtBridge
    from alarm.ui.calibration import CalibrationDialog

    manager = DeviceManager(SimulatedLink(), auto_ack=True)
    bridge = QtBridge()
    dialog = CalibrationDialog(window, manager=manager, bridge=bridge)
    try:
        dialog._prep_s.setValue(0)
        dialog._quiet_s.setValue(20)
        dialog._motion_s.setValue(35)
        dialog._start_prep()
        qapp.processEvents()

        dialog._show_intro()
        qapp.processEvents()

        assert dialog._prep_s.value() == 0
        assert dialog._quiet_s.value() == 20
        assert dialog._motion_s.value() == 35
    finally:
        dialog.close()
        manager.stop(timeout=0.5)


def test_un_paro_lento_no_cierra_la_fase_siguiente(window) -> None:
    """El plazo de 1,5 s de «Detener» no debe cortar la fase de movimiento."""
    from alarm.ui.bridge import QtBridge
    from alarm.ui.calibration import CalibrationDialog

    manager = DeviceManager(SimulatedLink(), auto_ack=True)
    manager.start()
    bridge = QtBridge()
    dialog = CalibrationDialog(window, manager=manager, bridge=bridge)
    try:
        dialog._durations = {"prep": 0, "quiet": 10, "motion": 10}
        dialog._start_phase("quiet", 10.0, dialog._start_motion)
        token = dialog._phase_token
        dialog._complete_phase()
        assert dialog._phase_name == "motion"
        assert dialog._phase_active

        # Llega el plazo del `calib.stop` de la fase anterior.
        dialog._force_complete(token)
        assert dialog._phase_name == "motion"
        assert dialog._phase_active
    finally:
        dialog.close()
        manager.stop(timeout=0.5)


def test_una_excepcion_en_un_slot_avisa_pero_no_cierra(window) -> None:
    """El gancho de `sys.excepthook` evita que PyQt6 aborte la aplicacion."""
    import sys

    from alarm.ui.app import _install_excepthook

    previous = sys.excepthook
    try:
        _install_excepthook(window)
        try:
            raise RuntimeError("fallo de prueba")
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        assert "fallo de prueba" in window._status_label.text()
    finally:
        sys.excepthook = previous


# ---------------------------------------------------------------------------
# Alarma y avisos
# ---------------------------------------------------------------------------


def test_menus_de_alarma_y_avisos(window) -> None:
    alarma = _labels(_menu(window, "Alarma"))
    assert alarma == [
        "Armar",
        "Desarmar",
        "Silenciar",
        "Probar zumbador",
        "Zumbador…",
        "Retardos y rearme…",
    ]

    avisos = _labels(_menu(window, "Avisos"))
    assert "Configuración de voces…" in avisos
    assert "Correo, SMS y móvil…" in avisos
    assert "Aviso al móvil (ntfy)" in avisos
    assert "Probar voz" in avisos
    assert "Recargar configuración" in avisos
    assert "Mensajes de voz" not in avisos, "la tabla única sustituye al submenú"

    herramientas = _labels(_menu(window, "Herramientas"))
    assert "Configuración de voces…" in herramientas
    assert "Zumbador…" in herramientas


def test_la_prueba_del_zumbador_se_salta_el_calentamiento(window) -> None:
    """El firmware calla durante el calentamiento: la prueba debe forzarlo.

    Sin `force`, el botón «Probar zumbador» nada más conectar no sonaba y
    parecía un cableado mal puesto.
    """
    from alarm.protocol.messages import Health

    window._manager = FakeManager(health=Health.ONLINE)

    window.test_buzzer()

    assert ("beep", {"name": "test", "ms": 0, "force": 1}) in window._manager.sent


def test_la_ventana_del_zumbador_prueba_y_guarda(window, tmp_path) -> None:
    from alarm.ui.dialogs.buzzer import BuzzerDialog

    window._manager = FakeManager()
    guardados: list[AppConfig] = []

    def guardar(config: AppConfig) -> None:
        guardados.append(config)
        window._save_settings(config)

    dialog = BuzzerDialog(
        window,
        config=window._config,
        on_save=guardar,
        on_pattern=window._play_buzzer_pattern,
        on_tone=window._play_buzzer_tone,
        connected=True,
        warming=True,
    )

    dialog._play_tone()
    assert (
        "beep",
        {"name": "tone", "ms": 1200, "force": 1, "freq": 2000},
    ) in window._manager.sent, "el tono continuo debe forzar el pitido"

    dialog._play_pattern()
    assert ("beep", {"name": "test", "ms": 0, "force": 1}) in window._manager.sent

    dialog._siren.setValue(30)
    dialog._hz.setValue(2700)
    dialog._tone_ms.setValue(800)
    dialog.accept()

    assert guardados, "guardar debe entregar la configuración"
    assert guardados[0].buzzer.siren_ms == 30_000
    assert guardados[0].buzzer.test_hz == 2700
    assert guardados[0].buzzer.test_ms == 800
    assert _local_config(window, tmp_path).buzzer.siren_ms == 30_000


def test_la_prueba_adelanta_el_interruptor_del_zumbador(window) -> None:
    """Si se marca la casilla y aún no se ha guardado, la placa debe obedecer."""
    window._manager = FakeManager()
    window._config = replace(window._config, buzzer=replace(window._config.buzzer, enabled=False))

    window._play_buzzer_pattern("armed", 0, False)
    assert window._manager.sent == [], "con la casilla apagada no se prueba nada"

    window._play_buzzer_pattern("armed", 0, True)
    assert ("set", {"buzzer": 1}) in window._manager.sent
    assert ("beep", {"name": "armed", "ms": 0, "force": 1}) in window._manager.sent


def test_guardar_el_zumbador_reenvia_la_sirena_a_la_placa(window) -> None:
    window._manager = FakeManager()
    window._save_settings(
        replace(window._config, buzzer=replace(window._config.buzzer, siren_ms=4000))
    )

    assert ("set", {"armed": 0, "buzzer": 1, "siren_ms": 4000}) in window._manager.sent


def test_armar_desarmar_desde_la_ventana(window) -> None:
    assert window._hero._state.text() == "DESARMADA"

    window.arm()
    assert window._hero._state.text().startswith("ARMADA")
    assert window._controller.armed is True
    assert window._speech.spoken, "armar debe anunciarse por voz"
    assert "armada" in window._speech.spoken[0].lower()
    assert window._primary.text() == "Desarmar"

    window.disarm()
    assert window._hero._state.text() == "DESARMADA"
    assert len(window._speech.spoken) == 2


def test_deteccion_armada_dispara_y_se_silencia(window) -> None:
    from alarm.protocol.messages import MotionEvent, MotionKind

    window.arm()
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    assert window._hero._state.text() == "DISPARADA"

    window.silence()
    assert window._hero._state.text() == "SILENCIADA"
    assert window._controller.armed is True, "silenciar no debe desarmar"


def test_deteccion_desarmada_no_dispara(window) -> None:
    from alarm.protocol.messages import MotionEvent, MotionKind

    antes = len(window._speech.spoken)
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    assert window._hero._state.text() == "DESARMADA"
    assert len(window._speech.spoken) == antes


def test_el_evento_se_confirma_solo_despues_de_registrarlo(window) -> None:
    """El ACK va al final: recibir no es lo mismo que haber procesado."""
    from alarm.protocol.messages import MotionEvent, MotionKind

    manager = FakeManager()
    window._manager = manager
    order: list[str] = []
    original = window._log_event

    def log_event(tag: str, kind: str, detail: str) -> None:
        order.append("log")
        original(tag, kind, detail)

    window._log_event = log_event  # type: ignore[method-assign]
    window.disarm()

    window._on_message(MotionEvent(seq=7, kind=MotionKind.START, zone=1, ms=1000))
    assert manager.acked == [7]
    assert order == ["log"], "no se confirma antes de registrar"

    # El fin del pulso tambien se confirma: si no, el buffer se llenaria.
    window._on_message(MotionEvent(seq=8, kind=MotionKind.END, zone=1, ms=1200, dur_ms=200))
    assert manager.acked == [7, 8]


def test_sin_gestor_el_evento_no_revienta_la_ventana(window) -> None:
    from alarm.protocol.messages import MotionEvent, MotionKind

    window._manager = None
    window.disarm()
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    assert window._hero._state.text() == "DESARMADA"


# ---------------------------------------------------------------------------
# Estado operativo persistente (H-02)
# ---------------------------------------------------------------------------


def _ventana_con_estado(tmp_path, *, armed: str | None, **alarm_overrides: object):
    """Ventana nueva sobre una base ya sembrada con `armed_intent`."""
    from alarm.services.storage import EventStore
    from alarm.ui.app import MainWindow

    path = tmp_path / "alarm.db"
    store = EventStore(path)
    if armed is not None:
        store.set_state("armed_intent", armed)
        store.set_state("armed_at", "1700000000")
    store.close()

    base = default_config()
    overrides: dict[str, object] = {"exit_delay_s": 0, "entry_delay_s": 0}
    overrides.update(alarm_overrides)
    instance = MainWindow(
        simulated=True,
        autoconnect=False,
        config=replace(base, alarm=replace(base.alarm, **overrides)),  # type: ignore[arg-type]
        config_path=tmp_path / "alarm.toml",
        local_path=tmp_path / "alarm.local.toml",
        env_path=tmp_path / ".env",
        history_path=path,
    )
    instance._speech = FakeSpeech()  # type: ignore[assignment]
    instance._notifier = FakeNotifier()  # type: ignore[assignment]
    return instance


def test_una_base_armada_restaura_armada(tmp_path) -> None:
    from alarm.core.state import AlarmState

    instance = _ventana_con_estado(tmp_path, armed="true")
    try:
        assert instance._controller.state is AlarmState.ARMED
        assert instance._hero._state.text().startswith("ARMADA")
        # La fila deja dicho de donde viene el estado.
        details = [row.detail for row in instance._store.recent(20)]
        assert any("Estado recuperado: ARMADA" in detail for detail in details)
    finally:
        instance.shutdown()
        instance.deleteLater()


def test_el_estado_guardado_manda_sobre_armed_on_start(tmp_path) -> None:
    """`armed_on_start=false` no puede desarmar lo que estaba armado."""
    from alarm.core.state import AlarmState

    instance = _ventana_con_estado(tmp_path, armed="true", armed_on_start=False)
    try:
        assert instance._controller.state is AlarmState.ARMED
    finally:
        instance.shutdown()
        instance.deleteLater()


def test_sin_estado_guardado_se_usa_armed_on_start(tmp_path) -> None:
    from alarm.core.state import AlarmState

    instance = _ventana_con_estado(tmp_path, armed=None, armed_on_start=True)
    try:
        assert instance._controller.state is AlarmState.ARMED
    finally:
        instance.shutdown()
        instance.deleteLater()


def test_arrancar_con_restore_desactivado_ignora_la_base(tmp_path) -> None:
    from alarm.core.state import AlarmState

    instance = _ventana_con_estado(
        tmp_path, armed="true", armed_on_start=False, restore_state_on_start=False
    )
    try:
        assert instance._controller.state is AlarmState.DISARMED
    finally:
        instance.shutdown()
        instance.deleteLater()


def test_desarmar_persiste_el_estado(tmp_path) -> None:
    instance = _ventana_con_estado(tmp_path, armed="true")
    try:
        instance.disarm()
        assert instance._store.get_state("armed_intent") == "false"
        assert instance._store.get_state("armed_at") is None
    finally:
        instance.shutdown()
        instance.deleteLater()


def test_armar_persiste_el_estado(tmp_path) -> None:
    instance = _ventana_con_estado(tmp_path, armed="false")
    try:
        instance.arm()
        assert instance._store.get_state("armed_intent") == "true"
        assert instance._store.get_state("armed_at") is not None
    finally:
        instance.shutdown()
        instance.deleteLater()


def test_una_base_ilegible_degrada_a_armed_on_start(tmp_path) -> None:
    from alarm.core.state import AlarmState

    path = tmp_path / "alarm.db"
    path.write_bytes(b"esto no es una base sqlite")
    from alarm.ui.app import MainWindow

    base = default_config()
    instance = MainWindow(
        simulated=True,
        autoconnect=False,
        config=replace(
            base,
            alarm=replace(base.alarm, armed_on_start=True, exit_delay_s=0, entry_delay_s=0),
        ),
        config_path=tmp_path / "alarm.toml",
        local_path=tmp_path / "alarm.local.toml",
        env_path=tmp_path / ".env",
        history_path=path,
    )
    instance._speech = FakeSpeech()  # type: ignore[assignment]
    instance._notifier = FakeNotifier()  # type: ignore[assignment]
    try:
        assert instance._controller.state is AlarmState.ARMED
        assert instance._store_error, "la ventana debe seguir diciendo que no hay historial"
    finally:
        instance.shutdown()
        instance.deleteLater()


def test_la_etiqueta_distingue_calentando_de_vigilancia(window) -> None:
    """Nunca se anuncia «vigilancia operativa» sin saludo y calentamiento."""
    from alarm.protocol.messages import Health, Hello, Ready, Zone

    hello = Hello(
        proto=1,
        dev="Arduino Mega 2560",
        fw="1.0",
        boot_ms=0,
        zones=(Zone(id=1, name="PIR", pin=2),),
        caps=("pir",),
    )
    window.arm()
    assert window._hero._state.text() == "ARMADA · SENSOR NO DISPONIBLE"

    manager = FakeManager(health=Health.ONLINE)
    manager.hello = hello  # type: ignore[attr-defined]
    window._manager = manager
    window._last_health = Health.ONLINE
    window._start_warmup(60_000)
    window._paint_alarm()
    assert window._hero._state.text() == "ARMADA · SENSOR CALENTÁNDOSE"

    window._on_message(Ready(warmup_ms=0))
    window._paint_alarm()
    assert window._hero._state.text() == "ARMADA · VIGILANCIA OPERATIVA"

    # Al caer el enlace vuelve a decir la verdad, sin desarmar.
    window._last_health = Health.OFFLINE
    window._paint_alarm()
    assert window._hero._state.text() == "ARMADA · SENSOR NO DISPONIBLE"


def test_armar_encola_el_aviso(window, qapp) -> None:
    window._config = _config_con_correo()
    window._apply_config(window._config)
    fake = window._notifier
    window.arm()
    assert [cause for cause, _, _ in fake.submitted] == ["armed"]
    subject, body = fake.submitted[0][1], fake.submitted[0][2]
    assert subject.startswith("[Alarma]")
    assert "ARMADA" in body


def test_el_evento_armado_se_puede_desactivar_por_canal(window) -> None:
    """El filtro por evento vive en el canal, no en la ventana."""
    config = _config_con_correo(on_armed=False)
    assert config.email.enabled_for("armed") is False
    assert config.email.enabled_for("triggered") is True

    window._config = config
    window._apply_config(config)
    window._notifier = FakeNotifier(channels=[])  # type: ignore[assignment]
    fake = window._notifier
    window.arm()
    assert fake.submitted == []


def test_la_etiqueta_de_estado_es_configurable(window) -> None:
    labels = dict(window._config.ui.labels)
    labels["armed"] = "EN GUARDIA"
    window._apply_config(replace(window._config, ui=replace(window._config.ui, labels=labels)))
    window.arm()
    # La etiqueta configurable manda; el sufijo de disponibilidad se anade.
    assert window._hero._state.text().startswith("EN GUARDIA")


# ---------------------------------------------------------------------------
# Dialogos
# ---------------------------------------------------------------------------


def test_tabla_de_voces_guarda_todos_los_eventos(window) -> None:
    from alarm.config import VOICE_KEYS
    from alarm.ui.dialogs.voice_config import VoiceConfigDialog

    guardados: list[AppConfig] = []
    dialog = VoiceConfigDialog(
        window,
        config=window._config,
        on_save=guardados.append,
        on_try=lambda *args: None,
    )
    assert dialog._table.rowCount() == len(VOICE_KEYS)

    row = dialog._rows["triggered"]
    row.text.setText("Intruso a las {hora}")
    row.label.setText("ALERTA")
    row.repeat.setValue(3)
    row.delay.setValue(2.5)
    row.gap.setValue(1.5)
    row.log.setChecked(False)
    dialog.accept()

    assert guardados, "guardar debe entregar la configuracion"
    event = guardados[0].tts.event("triggered")
    assert event.text == "Intruso a las {hora}"
    assert event.label == "ALERTA"
    assert event.repeat == 3
    assert event.delay_ms == 2500
    assert event.gap_ms == 1500
    assert event.log is False
    assert guardados[0].ui.label("triggered") == "ALERTA"
    assert set(guardados[0].tts.events) == set(VOICE_KEYS)


def test_tabla_de_voces_restaura_los_valores_de_fabrica(window) -> None:
    from alarm.ui.dialogs.voice_config import VoiceConfigDialog

    config = replace(
        window._config,
        tts=replace(
            window._config.tts,
            events={
                **window._config.tts.events,
                "armed": replace(
                    window._config.tts.event("armed"),
                    text="otra cosa",
                    repeat=7,
                    gap_ms=3000,
                    log=False,
                ),
            },
        ),
    )
    dialog = VoiceConfigDialog(window, config=config, on_save=lambda _c: None)
    dialog._reset()
    row = dialog._rows["armed"]
    assert row.text.text() == "Alarma armada."
    assert row.label.text() == "ARMADA"
    assert row.repeat.value() == 2
    assert row.gap.value() == 0.0
    assert row.log.isChecked() is True


def test_voz_desactivada_en_el_registro_no_apunta_la_locucion(window) -> None:
    config = replace(
        window._config,
        tts=replace(
            window._config.tts,
            events={
                **window._config.tts.events,
                "armed": replace(window._config.tts.event("armed"), log=False),
            },
        ),
    )
    window._apply_config(config)
    window.arm()

    assert window._speech.spoken, "el aviso se sigue pronunciando"
    filas = window._log.rows()
    assert any("ARMADA" in fila[2] for fila in filas), filas
    assert not any("Alarma armada." in fila[2] for fila in filas), filas


def test_dialogo_de_notificaciones_valida_y_guarda(window, tmp_path) -> None:
    from alarm.ui.dialogs.notifications import NotificationsDialog

    guardados: list[tuple[AppConfig, dict[str, str]]] = []
    dialog = NotificationsDialog(
        window,
        config=window._config,
        on_save=lambda config, secrets: guardados.append((config, secrets)),
    )
    dialog._email_enabled.setChecked(True)
    dialog._email_to.set_values([])
    dialog.accept()
    assert guardados == [], "sin destinatarios no debe guardar"

    dialog._email_to.add_value("yo@example.com")
    dialog._email_from.setText("alarma@example.com")
    dialog._email_host.setText("smtp.example.com")
    dialog._email_user.setText("cuenta@example.com")
    dialog._email_password.setText("clave")
    dialog._sms_enabled.setChecked(True)
    dialog._sms_provider.setCurrentIndex(dialog._sms_provider.findData("twilio"))
    dialog._sms_to.add_value("+34 600 11 22 33")
    dialog._sms_from.setText("+34600000000")
    dialog._sms_sid.setText("AC123")
    dialog._sms_token.setText("token")
    dialog._ntfy_enabled.setChecked(True)
    dialog._generate_topic()
    dialog._ntfy_token.setText("tk-secreto")
    dialog.accept()

    assert len(guardados) == 1
    config, secrets = guardados[0]
    assert config.email.to == ("yo@example.com",)
    assert config.sms.to == ("+34600112233",)
    assert config.sms.from_number == "+34600000000"
    assert config.ntfy.enabled is True
    assert config.ntfy.topic.startswith("alarma-")
    assert config.ntfy.configured is True
    assert secrets["ALARM_SMTP_PASSWORD"] == "clave"
    assert secrets["ALARM_TWILIO_SID"] == "AC123"
    assert secrets["ALARM_NTFY_TOKEN"] == "tk-secreto"


def test_probar_sms_sin_datos_avisa_y_no_envia(window, monkeypatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    from alarm.ui.dialogs.notifications import NotificationsDialog

    enviados: list[AppConfig] = []
    dialog = NotificationsDialog(
        window,
        config=window._config,
        on_save=lambda *_args: None,
        on_test_sms=enviados.append,
    )
    avisos: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **kwargs: avisos.append(args[2] if len(args) > 2 else "")),
    )
    try:
        dialog._sms_enabled.setChecked(True)
        dialog._sms_provider.setCurrentIndex(dialog._sms_provider.findData("twilio"))
        dialog._sms_to.set_values(["+34600111222"])
        dialog._sms_sid.setText("")
        dialog._sms_token.setText("")
        dialog._sms_from.setText("")

        dialog._test_sms()

        assert enviados == [], "sin credenciales no se llama al envio"
        assert avisos, "debe explicar que falta"
        assert "cuenta SID" in avisos[-1]
        assert "número remitente" in avisos[-1]
        assert "cuenta SID" in dialog._sms_status.text()
    finally:
        dialog.close()


def test_el_numero_escrito_sin_pulsar_anadir_cuenta_para_probar(window) -> None:
    from alarm.ui.dialogs.notifications import NotificationsDialog

    enviados: list[AppConfig] = []
    dialog = NotificationsDialog(
        window,
        config=window._config,
        on_save=lambda *_args: None,
        on_test_sms=enviados.append,
    )
    try:
        dialog._sms_enabled.setChecked(True)
        dialog._sms_provider.setCurrentIndex(dialog._sms_provider.findData("twilio"))
        dialog._sms_sid.setText("AC123")
        dialog._sms_token.setText("token")
        dialog._sms_from.setText("+34 600 000 000")
        dialog._sms_to.set_values([])
        dialog._sms_to._input.setText("+34 600 11 22 33")

        dialog._test_sms()

        assert len(enviados) == 1, "el número tecleado debe contar aunque no se pulse Añadir"
        assert enviados[0].sms.to == ("+34600112233",)
        assert enviados[0].sms.from_number == "+34600000000"
        assert dialog._sms_to.values() == ["+34 600 11 22 33"], "el editor conserva lo escrito"
    finally:
        dialog.close()


def test_el_estado_del_sms_dice_lo_que_falta(window) -> None:
    from alarm.ui.dialogs.notifications import NotificationsDialog

    config = replace(
        window._config,
        sms=replace(window._config.sms, enabled=True, provider="twilio"),
    )
    dialog = NotificationsDialog(window, config=config, on_save=lambda *_args: None)
    try:
        dialog._sms_to.set_values(["+34600111222"])
        dialog._sms_sid.setText("")
        dialog._sms_token.setText("")
        dialog._sms_from.setText("")
        dialog._refresh_sms_status()
        texto = dialog._sms_status.text()
        assert "cuenta SID" in texto
        assert "token de autenticación" in texto
        assert "número remitente" in texto

        dialog._sms_sid.setText("AC123")
        dialog._sms_token.setText("token")
        dialog._sms_from.setText("+34600000000")
        dialog._refresh_sms_status()
        assert "Listo" in dialog._sms_status.text()
    finally:
        dialog.close()


def test_el_estado_del_movil_avisa_del_tema_publico(window) -> None:
    from alarm.ui.dialogs.notifications import NotificationsDialog

    dialog = NotificationsDialog(window, config=window._config, on_save=lambda *_args: None)
    try:
        dialog._ntfy_topic.setText("")
        dialog._refresh_ntfy_status()
        assert "Falta: tema" in dialog._ntfy_status.text()

        dialog._ntfy_topic.setText("casa")
        dialog._refresh_ntfy_status()
        texto = dialog._ntfy_status.text()
        assert "Aviso" in texto
        assert "adivinar" in texto, "un tema corto en ntfy.sh no es un secreto"

        dialog._generate_topic()
        dialog._refresh_ntfy_status()
        texto = dialog._ntfy_status.text()
        assert "adivinar" not in texto
        assert "público" in texto, "sin token, el tema sigue siendo publico"

        dialog._ntfy_token.setText("tk")
        dialog._refresh_ntfy_status()
        assert "Listo" in dialog._ntfy_status.text()
    finally:
        dialog.close()


def test_probar_movil_llama_al_callback(window) -> None:
    from alarm.ui.dialogs.notifications import NotificationsDialog

    enviados: list[AppConfig] = []
    dialog = NotificationsDialog(
        window,
        config=window._config,
        on_save=lambda *_args: None,
        on_test_ntfy=enviados.append,
    )
    try:
        dialog._ntfy_topic.setText("casa-abc123456")
        dialog._test_ntfy()
        assert len(enviados) == 1
        assert enviados[0].ntfy.topic == "casa-abc123456"
    finally:
        dialog.close()


def test_el_boton_de_ntfy_abre_la_guia(window, monkeypatch) -> None:
    from alarm.ui.dialogs import notifications as module

    abiertas: list[str] = []
    monkeypatch.setattr(
        module.QDesktopServices,
        "openUrl",
        staticmethod(lambda url: abiertas.append(url.toString())),
    )
    dialog = module.NotificationsDialog(window, config=window._config, on_save=lambda *_a: None)
    try:
        dialog._open_ntfy_docs()
        assert abiertas == [module.NTFY_DOCS]
    finally:
        dialog.close()


def test_el_boton_de_twilio_abre_la_consola(window, monkeypatch) -> None:
    from alarm.ui.dialogs import notifications as module

    abiertas: list[str] = []
    monkeypatch.setattr(
        module.QDesktopServices,
        "openUrl",
        staticmethod(lambda url: abiertas.append(url.toString())),
    )
    dialog = module.NotificationsDialog(window, config=window._config, on_save=lambda *_a: None)
    try:
        dialog._open_twilio()
        assert abiertas == [module.TWILIO_CONSOLE]
    finally:
        dialog.close()


def test_dialogo_de_notificaciones_conserva_la_contrasena_si_se_deja_vacia(window) -> None:
    from alarm.ui.dialogs.notifications import NotificationsDialog

    config = replace(
        window._config,
        email=replace(window._config.email, password="guardada", enabled=True),
    )
    guardados: list[tuple[AppConfig, dict[str, str]]] = []
    dialog = NotificationsDialog(
        window,
        config=config,
        on_save=lambda cfg, secrets: guardados.append((cfg, secrets)),
    )
    dialog._email_to.set_values(["yo@example.com"])
    dialog._email_from.setText("alarma@example.com")
    dialog._email_host.setText("smtp.example.com")
    dialog._email_user.setText("cuenta@example.com")
    dialog.accept()

    assert len(guardados) == 1
    assert guardados[0][0].email.password == "guardada"
    assert "ALARM_SMTP_PASSWORD" not in guardados[0][1]


def test_dialogo_de_notificaciones_casa_puerto_y_cifrado(window) -> None:
    from alarm.ui.dialogs.notifications import NotificationsDialog

    dialog = NotificationsDialog(window, config=window._config, on_save=lambda *_args: None)
    try:
        dialog._email_security.setCurrentIndex(dialog._email_security.findData("ssl"))
        assert dialog._email_port.value() == 465
        dialog._email_port.setValue(587)
        assert dialog._email_security.currentData() == "starttls"

        # Un puerto no convencional respeta lo elegido a mano…
        dialog._email_security.setCurrentIndex(dialog._email_security.findData("ssl"))
        dialog._email_port.setValue(2525)
        assert dialog._email_security.currentData() == "ssl"

        # …pero al guardar manda la coherencia: 2525 no habla SSL implícito.
        dialog._email_to.set_values(["yo@example.com"])
        dialog._email_from.setText("alarma@example.com")
        dialog._email_host.setText("smtp.example.com")
        dialog._email_user.setText("cuenta@example.com")
        dialog._email_password.setText("clave")
        assert dialog.build_config().email.security == "starttls"
    finally:
        dialog.close()


def test_guardar_aplica_y_escribe_la_capa_local(window, tmp_path) -> None:
    labels = dict(window._config.ui.labels)
    labels["disarmed"] = "APAGADA"
    config = replace(window._config, ui=replace(window._config.ui, labels=labels))

    window._save_settings(config, {"ALARM_SMTP_USER": "cuenta@example.com"})

    local = tmp_path / "alarm.local.toml"
    env = tmp_path / ".env"
    assert local.is_file()
    assert "APAGADA" in local.read_text(encoding="utf-8")
    assert "ALARM_SMTP_USER" in env.read_text(encoding="utf-8")
    assert window._hero._state.text() == "APAGADA"


def test_acerca_de_muestra_el_correo_de_contacto(window) -> None:
    from alarm.ui.dialogs.about import CONTACT_EMAIL, AboutDialog

    dialog = AboutDialog(window)
    try:
        assert dialog.windowTitle() == "Acerca de la alarma"
        texts = [label.text() for label in dialog.findChildren(type(dialog._feedback))]
        joined = " ".join(texts)
        assert CONTACT_EMAIL in joined
    finally:
        dialog.close()


# ---------------------------------------------------------------------------
# Comprobaciones auxiliares
# ---------------------------------------------------------------------------


def test_el_resultado_de_una_prueba_se_muestra(window, monkeypatch) -> None:
    from alarm.ui.dialogs import result as result_module

    mostrados: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        result_module.ResultDialog,
        "exec",
        lambda self: mostrados.append((self.windowTitle(), True)),
    )
    window._on_test_finished("correo", DeliveryResult("email", True, "enviado a yo@example.com"))
    assert mostrados == [("Prueba de correo", True)]


# ---------------------------------------------------------------------------
# Conexion permanente y aviso de perdida
# ---------------------------------------------------------------------------


class FakeManager:
    """Gestor minimo para probar la ventana sin abrir un hilo."""

    def __init__(
        self,
        *,
        ever_online: bool = True,
        health: object = None,
        alive: bool = True,
        error: str = "cable fuera",
    ) -> None:
        from types import SimpleNamespace

        from alarm.protocol.messages import Health

        self.ever_online = ever_online
        self.health = Health.OFFLINE if health is None else health
        self.hello = None
        self.alive = alive
        self.stats = SimpleNamespace(last_error=error)
        self.next_retry_in = 7.0
        self.heartbeat_age_s = 26.0
        self.link = None
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.acked: list[int] = []

    def send(self, op: str, **params: object) -> int:
        self.sent.append((op, params))
        return 1

    def ack_events(self, seq: int) -> None:
        self.acked.append(seq)

    def stop(self, timeout: float = 3.0) -> None:
        pass


def test_el_boton_de_conexion_tiene_los_tres_estados(window) -> None:
    from alarm.protocol.messages import Health

    window._manager = FakeManager(health=Health.ONLINE)
    window._paint_connection(Health.ONLINE)
    boton = window._connect_button
    assert boton.text() == "Conectado"
    assert boton.isEnabled() is False, "conectado no es una accion"
    assert boton.property("state") == "connected"

    window._paint_connection(Health.OFFLINE)
    assert boton.text() == "Reconectando…"
    assert boton.property("state") == "retrying"
    assert "7" in boton.toolTip(), "el tooltip dice cuanto falta para el reintento"

    window._manager = None
    window._paint_connection(None)
    assert boton.text() == "Conectar"
    assert boton.isEnabled() is True
    assert boton.property("state") == "offline"


def test_perder_el_enlace_avisa_por_registro_voz_y_correo(window) -> None:
    from alarm.protocol.messages import Health

    window._config = _config_con_correo()
    window._apply_config(window._config)
    notifier = window._notifier
    window._manager = FakeManager(ever_online=True, health=Health.OFFLINE)

    window._on_health(Health.OFFLINE)

    filas = window._log.rows()
    assert any("Sin sensor" in fila[1] for fila in filas), filas
    assert any("device_lost" in key for key in window._speech.pending)
    assert [cause for cause, _, _ in notifier.submitted] == ["device_lost"]
    assert "desconectado" in " ".join(window._speech.spoken).lower()
    assert window._link_down is True
    assert window._device_warning.isVisibleTo(window)


def test_perder_el_enlace_avisa_una_sola_vez(window) -> None:
    from alarm.protocol.messages import Health

    window._config = _config_con_correo()
    window._apply_config(window._config)
    notifier = window._notifier
    window._manager = FakeManager(ever_online=True, health=Health.OFFLINE)

    window._on_health(Health.OFFLINE)
    window._on_health(Health.OFFLINE)
    window._on_health(Health.OFFLINE)

    assert len(notifier.submitted) == 1
    assert len(window._speech.pending) == 1


def test_arrancar_sin_placa_no_avisa(window) -> None:
    from alarm.protocol.messages import Health

    window._config = _config_con_correo()
    window._apply_config(window._config)
    notifier = window._notifier
    window._manager = FakeManager(ever_online=False, health=Health.UNKNOWN)

    window._on_health(Health.OFFLINE)

    assert notifier.submitted == []
    assert window._speech.pending == []
    assert window._log.rows() == []
    assert "esperando" in window._status_label.text().lower()


def test_recuperar_el_enlace_anuncia_que_el_sensor_vuelve(window) -> None:
    from alarm.protocol.messages import Health

    window._manager = FakeManager(ever_online=True, health=Health.OFFLINE)
    window._on_health(Health.OFFLINE)
    window._config = replace(
        window._config,
        tts=replace(
            window._config.tts,
            events={
                **window._config.tts.events,
                "device_ready": replace(window._config.tts.event("device_ready"), enabled=True),
            },
        ),
    )
    window._speech = FakeSpeech()  # type: ignore[assignment]

    window._on_health(Health.ONLINE)

    assert any("recuperado" in fila[1].lower() for fila in window._log.rows())
    assert "device_ready" in window._speech.pending
    assert window._link_lost is False
    assert window._device_warning.isVisibleTo(window) is False


def test_desconectar_a_mano_no_dispara_el_aviso(window) -> None:
    from alarm.protocol.messages import Health

    window._config = _config_con_correo()
    window._apply_config(window._config)
    notifier = window._notifier
    window._manager = FakeManager(ever_online=True, health=Health.ONLINE)

    window.disconnect()
    window._on_health(Health.OFFLINE)

    assert notifier.submitted == []
    assert window._speech.pending == []


# ---------------------------------------------------------------------------
# Historial y exportacion
# ---------------------------------------------------------------------------


def test_el_historial_sobrevive_a_reabrir_la_ventana(qapp, tmp_path) -> None:
    from alarm.ui.app import MainWindow

    primero = MainWindow(
        simulated=True,
        autoconnect=False,
        config=default_config(),
        config_path=tmp_path / "alarm.toml",
        local_path=tmp_path / "alarm.local.toml",
        env_path=tmp_path / ".env",
        history_path=tmp_path / "alarm.db",
    )
    try:
        primero._log_event("info", "Prueba", "sobrevive al cierre")
    finally:
        primero.shutdown()
        primero.deleteLater()

    segundo = MainWindow(
        simulated=True,
        autoconnect=False,
        config=default_config(),
        config_path=tmp_path / "alarm.toml",
        local_path=tmp_path / "alarm.local.toml",
        env_path=tmp_path / ".env",
        history_path=tmp_path / "alarm.db",
    )
    try:
        rows = segundo._log.rows()
        assert any(fila[1] == "Prueba" and "sobrevive" in fila[2] for fila in rows)
        assert "de 1" in segundo._log_footer.text()
    finally:
        segundo.shutdown()
        segundo.deleteLater()


def test_el_arbol_respeta_el_limite_visible(window) -> None:
    window._log.set_limit(2)
    for index in range(5):
        window._log.add_row("info", f"evento {index}", "detalle")
    assert window._log.topLevelItemCount() == 2
    assert [fila[1] for fila in window._log.rows()] == ["evento 3", "evento 4"]


def test_el_arbol_no_crece_mas_alla_del_limite(window) -> None:
    window._log.set_limit(3)
    for index in range(6):
        window._log_event("info", f"evento {index}", "detalle")
    assert window._log.topLevelItemCount() == 3
    assert window._store.count() == 6, "la base conserva todo aunque el arbol recorte"


def test_exportar_registro_escribe_un_csv_completo(window, tmp_path, monkeypatch) -> None:
    from alarm.ui import app as app_module

    window._log_event("info", "Prueba, con coma", 'Detalle "entrecomillado"')
    destino = tmp_path / "registro.csv"
    monkeypatch.setattr(
        app_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(destino), "CSV (*.csv)")),
    )

    window.export_log()

    assert destino.is_file()
    contenido = destino.read_text(encoding="utf-8-sig")
    assert contenido.splitlines()[0].startswith("fecha_local")
    assert "Prueba, con coma" in contenido
    assert "entrecomillado" in contenido
    assert str(destino) in window._status_label.text()


def test_exportar_cancelado_no_hace_nada(window, monkeypatch) -> None:
    from alarm.ui import app as app_module

    monkeypatch.setattr(
        app_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: ("", "")),
    )
    window.export_log()
    assert window._store.count() == 0, "el historial no cambia al cancelar"


def test_la_cabecera_no_repite_el_estado_de_conexion(window) -> None:
    """El chip «Conectado» se quito: el estado vive en la tarjeta Dispositivo."""
    from PyQt6.QtWidgets import QLabel

    assert not hasattr(window, "_chip")
    chips = [label for label in window.findChildren(QLabel) if label.objectName() == "chip"]
    assert chips == [], "no debe quedar ninguna píldora de conexión"


def test_la_tarjeta_muestra_la_salud_del_enlace(window) -> None:
    from alarm.protocol.messages import Health

    window._manager = FakeManager(health=Health.ONLINE)
    window._paint_link_status(Health.ONLINE)
    assert "Conectado" in window._link_status.text()
    assert "latido" in window._link_status.text()

    window._manager = FakeManager(health=Health.OFFLINE)
    window._paint_link_status(Health.OFFLINE)
    assert "próximo intento" in window._link_status.text()

    window._manager = None
    window._paint_link_status(None)
    assert "Sin enlace" in window._link_status.text()


def test_el_registro_se_ordena_por_cualquier_columna(window) -> None:
    from PyQt6.QtCore import Qt

    for kind, detail in (
        ("Zeta", "tercera"),
        ("Alfa", "primera"),
        ("Media", "segunda"),
    ):
        window._log.add_row("info", kind, detail, scroll=False)

    window._log.sortByColumn(1, Qt.SortOrder.AscendingOrder)
    assert [fila[1] for fila in window._log.rows()] == ["Alfa", "Media", "Zeta"]

    window._log.sortByColumn(2, Qt.SortOrder.DescendingOrder)
    assert [fila[2] for fila in window._log.rows()] == ["tercera", "segunda", "primera"]


def test_la_hora_se_ordena_por_instante_y_no_por_texto(window) -> None:
    """Cruzar medianoche rompe el orden alfabetico de `HH:MM:SS`."""
    from PyQt6.QtCore import Qt

    base = 1_760_000_000.0
    window._log.add_row("info", "Tarde", "23:59", ts=base + 60, scroll=False)
    window._log.add_row("info", "Temprano", "00:00", ts=base, scroll=False)
    window._log.sortByColumn(0, Qt.SortOrder.AscendingOrder)

    assert [fila[1] for fila in window._log.rows()] == ["Temprano", "Tarde"]


def test_el_limite_recorta_por_llegada_aunque_este_ordenado(window) -> None:
    """Ordenar por detalle no debe borrar la fila equivocada al recortar."""
    from PyQt6.QtCore import Qt

    window._log.set_limit(3)
    base = 1_760_000_000.0
    for index in range(5):
        window._log.add_row("info", f"evento {index}", "detalle", ts=base + index, scroll=False)

    window._log.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    window._log.add_row("info", "evento 9", "detalle", ts=base + 9, scroll=False)

    assert window._log.topLevelItemCount() == 3
    quedan = {fila[1] for fila in window._log.rows()}
    assert "evento 9" in quedan
    assert "evento 0" not in quedan, "se descarta la mas antigua, no la de la fila 0"
    assert "evento 2" not in quedan


def test_limpiar_pide_confirmacion(window, monkeypatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    from alarm.ui import app as app_module

    window._log_event("info", "Prueba", "se va a borrar")

    monkeypatch.setattr(
        app_module.QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No),
    )
    window._confirm_clear_log()
    assert window._store.count() == 1, "responder No no borra nada"

    monkeypatch.setattr(
        app_module.QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    window._confirm_clear_log()
    assert window._store.count() == 0
    assert window._log.topLevelItemCount() == 0


def test_el_panel_del_sensor_refleja_el_nivel(window) -> None:
    window._sensor.set_connected(True)
    window._paint_sensor(True)
    assert window._sensor.level is True

    window._paint_sensor(False)
    assert window._sensor.level is False
    assert window._sensor.energy >= 0.0


def test_el_latido_aviva_el_panel_del_sensor(window) -> None:
    from alarm.protocol.messages import Heartbeat

    window._sensor.set_connected(True)
    window._on_message(
        Heartbeat(
            up_ms=1000,
            level=True,
            raw_edges=0,
            buffered=0,
            lost=0,
            calibrating=False,
            warming_up=False,
        )
    )
    assert window._sensor.level is True
    assert window._sensor.activity.last_motion_age(time.time()) is not None


def test_la_barra_de_calentamiento_sigue_al_dispositivo(window) -> None:
    """La barra aparece con el Hello, avanza con el latido y se oculta en Ready."""
    from alarm.protocol.messages import Heartbeat, Hello, Ready, Zone

    window._on_message(
        Hello(
            proto=1,
            dev="Arduino Mega 2560",
            fw="1.2.0",
            boot_ms=0,
            zones=(Zone(1, "PIR", 2),),
            caps=(),
            cfg={"warmup_ms": 60000},
        )
    )
    assert window._warmup_bar.isVisibleTo(window) is True
    assert window._warmup_total_ms == 60000
    assert "en curso" in window._ready_label.text()

    window._on_message(
        Heartbeat(
            up_ms=30000,
            level=False,
            raw_edges=0,
            buffered=0,
            lost=0,
            calibrating=False,
            warming_up=True,
        )
    )
    assert window._warmup_bar.value() == 500, "a mitad de calentamiento, media barra"
    assert "30 s" in window._ready_label.text(), "los segundos se leen en la etiqueta"

    window._on_message(Ready(warmup_ms=60000))
    assert window._warmup_bar.isVisibleTo(window) is False
    assert "completado" in window._ready_label.text()


def test_la_barra_de_calentamiento_se_oculta_al_desconectar(window) -> None:
    from alarm.protocol.messages import Hello, Zone

    window._manager = FakeManager()
    window._on_message(
        Hello(
            proto=1,
            dev="Mega",
            fw="1.2.0",
            boot_ms=0,
            zones=(Zone(1, "PIR", 2),),
            caps=(),
            cfg={"warmup_ms": 60000},
        )
    )
    assert window._warmup_total_ms == 60000

    window.disconnect()

    assert window._warmup_total_ms == 0
    assert window._warmup_bar.isVisibleTo(window) is False


def test_sin_calentamiento_no_se_muestra_la_barra(window) -> None:
    from alarm.protocol.messages import Hello, Zone

    window._on_message(
        Hello(
            proto=1,
            dev="Mega",
            fw="1.2.0",
            boot_ms=0,
            zones=(Zone(1, "PIR", 2),),
            caps=(),
            cfg={"warmup_ms": 0},
        )
    )
    assert window._warmup_bar.isVisibleTo(window) is False
    assert "no requerido" in window._ready_label.text()


def test_omitir_calentamiento_esconde_la_barra(window) -> None:
    from alarm.protocol.messages import Hello, Zone

    window._manager = FakeManager()
    window._on_message(
        Hello(
            proto=1,
            dev="Mega",
            fw="1.2.0",
            boot_ms=0,
            zones=(Zone(1, "PIR", 2),),
            caps=(),
            cfg={"warmup_ms": 60000},
        )
    )
    window.skip_warmup()

    assert window._warmup_total_ms == 0
    assert window._warmup_bar.isVisibleTo(window) is False
    assert "omitido" in window._ready_label.text()
    assert ("set", {"warmup_ms": 0}) in window._manager.sent


# ---------------------------------------------------------------------------
# Configuracion persistente
# ---------------------------------------------------------------------------


def _local_config(window, tmp_path):
    """Relee la configuracion desde los mismos ficheros que la ventana."""
    from alarm.config import load_config, load_env

    return load_config(
        tmp_path / "alarm.toml",
        env=load_env(tmp_path / ".env"),
        local=tmp_path / "alarm.local.toml",
    )


def test_marcar_sms_se_guarda_en_disco(window, tmp_path) -> None:
    """El fallo original: el check cambiaba en memoria y se perdía al recargar."""
    assert window._config.sms.enabled is False

    window.act_sms.setChecked(True)

    assert window._config.sms.enabled is True
    assert _local_config(window, tmp_path).sms.enabled is True, "debe llegar al TOML local"

    window.reload_config()
    assert window.act_sms.isChecked() is True, "recargar no debe desmarcarlo"


def test_marcar_el_aviso_al_movil_se_guarda_en_disco(window, tmp_path) -> None:
    assert window._config.ntfy.enabled is False

    window.act_ntfy.setChecked(True)

    assert window._config.ntfy.enabled is True
    assert _local_config(window, tmp_path).ntfy.enabled is True

    window.reload_config()
    assert window.act_ntfy.isChecked() is True, "recargar no debe desmarcarlo"


def test_marcar_correo_y_voz_se_guarda_en_disco(window, tmp_path) -> None:
    window.act_email.setChecked(True)
    window.act_tts.setChecked(False)

    guardada = _local_config(window, tmp_path)
    assert guardada.email.enabled is True
    assert guardada.tts.enabled is False

    window.reload_config()
    assert window.act_email.isChecked() is True
    assert window.act_tts.isChecked() is False


#: Canal, accion del menu, seccion de `AppConfig` y mensaje de estado al cancelar.
_AVISOS_APAGABLES = (
    ("voz", "act_tts", "tts", "La voz sigue activada."),
    ("correo", "act_email", "email", "El correo sigue activado."),
    ("sms", "act_sms", "sms", "Los SMS siguen activados."),
)


@pytest.mark.parametrize(("_canal", "accion", "seccion", "mensaje"), _AVISOS_APAGABLES)
def test_apagar_un_aviso_pide_confirmacion(
    window, tmp_path, monkeypatch, _canal, accion, seccion, mensaje
) -> None:
    """Apagar un aviso es silencioso: se pregunta antes y «No» no cambia nada."""
    from PyQt6.QtWidgets import QMessageBox

    from alarm.ui import app as app_module

    ajuste = getattr(window, accion)
    ajuste.setChecked(True)
    ruta = tmp_path / "alarm.local.toml"
    antes = ruta.read_text(encoding="utf-8") if ruta.is_file() else ""

    monkeypatch.setattr(
        app_module.QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No),
    )
    ajuste.setChecked(False)

    assert ajuste.isChecked() is True, "responder No deja el aviso encendido"
    assert getattr(window._config, seccion).enabled is True
    assert window._status_label.text() == mensaje
    despues = ruta.read_text(encoding="utf-8") if ruta.is_file() else ""
    assert despues == antes, "si no hay cambio, no se reescribe el TOML"


@pytest.mark.parametrize(("_canal", "accion", "seccion", "mensaje"), _AVISOS_APAGABLES)
def test_confirmar_apaga_el_aviso_y_lo_guarda(window, tmp_path, _canal, accion, seccion, mensaje):
    ajuste = getattr(window, accion)
    ajuste.setChecked(True)
    ajuste.setChecked(False)  # el fixture responde Yes

    assert ajuste.isChecked() is False
    assert getattr(window._config, seccion).enabled is False
    assert getattr(_local_config(window, tmp_path), seccion).enabled is False


def test_la_barra_de_estado_muestra_los_seis_canales(window) -> None:
    assert list(window._channels) == ["voz", "correo", "sms", "movil", "mando", "zumbador"]
    for canal in window._channels.values():
        assert canal.pixmap().isNull() is False, "cada indicador debe tener icono"

    assert "activa" in window._channels["voz"].toolTip()
    window.act_tts.setChecked(False)
    assert "desactivada" in window._channels["voz"].toolTip()

    # El zumbador no tiene casilla en el menu: se apaga desde su dialogo y el
    # indicador debe seguir a la configuracion, no a una accion concreta.
    window._apply_config(
        replace(window._config, buzzer=replace(window._config.buzzer, enabled=False))
    )
    assert "desactivado" in window._channels["zumbador"].toolTip()


def test_un_canal_encendido_sin_datos_se_marca_en_ambar(window) -> None:
    """Correo marcado pero sin credenciales: no es lo mismo que apagado."""
    window.act_email.setChecked(True)

    tooltip = window._channels["correo"].toolTip()
    assert "pero faltan datos" in tooltip
    assert "Correo, SMS y móvil" in tooltip, "el tooltip debe decir donde se arregla"


def test_las_preferencias_de_vista_sobreviven_a_recargar(window, tmp_path) -> None:
    window.act_show_heartbeats.setChecked(True)
    window.act_beeps.setChecked(False)

    guardada = _local_config(window, tmp_path)
    assert guardada.ui.show_heartbeats is True
    assert guardada.ui.beeps is False

    window.reload_config()
    assert window.act_show_heartbeats.isChecked() is True
    assert window.act_beeps.isChecked() is False


def test_recargar_usa_los_ficheros_de_la_ventana(window, tmp_path) -> None:
    """`reload_config` debe leer las rutas de la ventana, no las globales."""
    (tmp_path / "alarm.local.toml").write_text("[notify.sms]\nenabled = true\n", encoding="utf-8")

    window.reload_config()

    assert window._config.sms.enabled is True
    assert window.act_sms.isChecked() is True


def test_si_el_guardado_falla_el_check_se_revierte(window, monkeypatch) -> None:
    from alarm.ui import app as app_module

    def explota(*_args, **_kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr(app_module, "save_local", explota)
    window.act_sms.setChecked(True)

    assert window.act_sms.isChecked() is False, "el menú no debe mentir"
    assert window._config.sms.enabled is False


def test_exportar_configuracion_a_json(window, tmp_path, monkeypatch) -> None:
    import json

    from alarm.ui import app as app_module

    destino = tmp_path / "config.json"
    monkeypatch.setattr(
        app_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(destino), "JSON (*.json)")),
    )
    window._config = replace(
        window._config,
        email=replace(window._config.email, user="yo@gmail.com", password="clave"),
        sms=replace(window._config.sms, sid="AC123", token="secreto"),
    )

    window.export_config()

    assert destino.is_file()
    data = json.loads(destino.read_text(encoding="utf-8"))
    for section in ("serial", "alarm", "tts", "email", "sms", "ui", "buzzer", "log"):
        assert section in data
    assert data["email"]["password"] == ""
    assert data["sms"]["token"] == ""
    assert data["sms"]["sid"] == "AC123"
    assert str(destino) in window._status_label.text()


def test_exportar_configuracion_cancelado_no_escribe(window, tmp_path, monkeypatch) -> None:
    from alarm.ui import app as app_module

    monkeypatch.setattr(
        app_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: ("", "")),
    )
    window.export_config()

    assert list(tmp_path.glob("*.json")) == []


def test_exportar_configuracion_anade_la_extension(window, tmp_path, monkeypatch) -> None:
    from alarm.ui import app as app_module

    destino = tmp_path / "sin-extension"
    monkeypatch.setattr(
        app_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(destino), "JSON (*.json)")),
    )
    window.export_config()

    assert (tmp_path / "sin-extension.json").is_file()
    assert not destino.exists()


def test_el_menu_archivo_ofrece_copia_completa_y_restaurar(window) -> None:
    titulos = [action.text() for action in _menu(window, "Archivo").actions()]

    assert any("copia completa" in titulo for titulo in titulos)
    assert any("Restaurar copia completa" in titulo for titulo in titulos)


def test_guardar_copia_completa_escribe_las_credenciales(window, tmp_path, monkeypatch) -> None:
    import json

    from PyQt6.QtWidgets import QMessageBox

    from alarm.ui import app as app_module

    window._config = replace(
        window._config,
        email=replace(window._config.email, user="yo@gmail.com", password="secreto"),
    )
    destino = tmp_path / "copia.alarmbackup"
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(
        app_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(destino), "Copia (*.alarmbackup)")),
    )

    window.export_backup()

    data = json.loads(destino.read_text(encoding="utf-8"))
    assert data["incluye_credenciales"] is True
    assert data["secretos"]["ALARM_SMTP_PASSWORD"] == "secreto"
    assert data["config"]["serial"]["port"] == window._config.serial.port
    assert "credenciales" in window._status_label.text()


def test_guardar_copia_se_puede_cancelar_en_el_aviso(window, tmp_path, monkeypatch) -> None:
    from PyQt6.QtWidgets import QMessageBox

    from alarm.ui import app as app_module

    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No),
    )
    monkeypatch.setattr(
        app_module.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(tmp_path / "no.alarmbackup"), "")),
    )

    window.export_backup()

    assert list(tmp_path.glob("*.alarmbackup")) == []


def test_restaurar_copia_aplica_la_configuracion(window, tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    from alarm.backup import build_backup, write_backup
    from alarm.ui import app as app_module

    copia = tmp_path / "copia.alarmbackup"
    origen = replace(
        window._config,
        serial=replace(window._config.serial, port="COM9", baudrate=57600),
        log=replace(window._config.log, visible_rows=250),
        email=replace(window._config.email, to=("otro@example.com",)),
    )
    write_backup(
        build_backup(
            origen,
            secrets={"ALARM_SMTP_PASSWORD": "clave-nueva"},
            machine="PC-ORIGEN",
            created="2026-11-12T20:00:00",
        ),
        copia,
    )
    monkeypatch.setattr(
        app_module.QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(copia), "Copia (*.alarmbackup)")),
    )

    window.restore_backup()

    assert window._config.serial.port == "COM9"
    assert window._config.log.visible_rows == 250
    assert window._config.email.to == ("otro@example.com",)
    assert (tmp_path / "alarm.local.toml").is_file()
    assert "ALARM_SMTP_PASSWORD=clave-nueva" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "restaurada" in window._status_label.text().lower()


def test_restaurar_un_fichero_ajeno_no_cambia_nada(window, tmp_path, monkeypatch) -> None:
    from alarm.ui import app as app_module

    ajeno = tmp_path / "otro.json"
    ajeno.write_text('{"formato": "otra-cosa"}', encoding="utf-8")
    monkeypatch.setattr(
        app_module.QFileDialog,
        "getOpenFileName",
        staticmethod(lambda *args, **kwargs: (str(ajeno), "JSON (*.json)")),
    )
    antes = window._config

    window.restore_backup()

    assert window._config is antes
    assert not (tmp_path / "alarm.local.toml").exists()


def test_la_barra_de_calentamiento_no_se_monta_en_ventana_minima(window) -> None:
    """Con la ventana en su tamano minimo, cada fila de la tarjeta tiene su sitio.

    Antes el minimo declarado (620 px de alto) era menor que el que pide el
    contenido: Qt repartia menos altura y la etiqueta del calentamiento se
    dibujaba encima de la barra y de los contadores.
    """
    from PyQt6.QtWidgets import QApplication

    window._warmup_total_ms = 60000
    window._warmup_base_ms = 0
    window._warmup_bar.setVisible(True)
    window._paint_warmup()
    window.resize(window.minimumSize())
    window.show()
    QApplication.processEvents()

    sensor = window._sensor.geometry()
    label = window._ready_label.geometry()
    bar = window._warmup_bar.geometry()
    counters = window._events_label.geometry()

    assert label.top() >= sensor.bottom(), "la etiqueta no puede pisar el panel del sensor"
    assert bar.top() >= label.bottom(), "la barra va debajo de la etiqueta, sin tocarla"
    assert counters.top() >= bar.bottom(), "los contadores van debajo de la barra"


def test_limpiar_registro_vacia_tambien_la_base(window) -> None:
    window._log_event("info", "Prueba", "se va a borrar")
    assert window._store.count() == 1

    window.clear_log()

    assert window._store.count() == 0
    assert window._log.topLevelItemCount() == 0


def test_un_lector_muerto_se_relanza_solo(window) -> None:
    """Un hilo que muere no debe dejar la alarma muda hasta reiniciar."""
    link = SimulatedLink(warmup_s=0.1, speed=20.0)
    manager = DeviceManager(link, auto_ack=True)
    manager.start()
    manager.stop(timeout=1.0)
    window._manager = manager

    window._refresh()

    relevo = window._manager
    assert relevo is not None and relevo is not manager
    assert relevo.alive is True
    window.disconnect()


# ---------------------------------------------------------------------------
# Retardos de entrada y salida
# ---------------------------------------------------------------------------


def _reloj(window, *, exit_s: int = 0, entry_s: int = 0, start: float = 0.0) -> list[float]:
    """Sustituye el controlador por uno con reloj falso, sin esperar de verdad."""
    from alarm.core.state import AlarmController

    now = [start]
    # Las pruebas de la ventana parten de una configuracion sin retardos; aqui
    # se sincroniza el TOML con el controlador para que los pitidos lleven la
    # duracion real, como en la aplicacion.
    window._config = replace(
        window._config,
        alarm=replace(window._config.alarm, exit_delay_s=exit_s, entry_delay_s=entry_s),
    )
    window._controller = AlarmController(
        exit_delay_s=exit_s,
        entry_delay_s=entry_s,
        clock=lambda: now[0],
    )
    window._paint_alarm()
    return now


def test_el_menu_de_alarma_ofrece_los_retardos(window) -> None:
    assert "Retardos y rearme…" in _labels(_menu(window, "Alarma"))


def test_el_retardo_de_salida_pita_y_anuncia_al_terminar(window) -> None:
    window._manager = FakeManager()
    now = _reloj(window, exit_s=20)

    window.arm()

    assert window._hero._state.text() == "ARMANDO"
    assert window._controller.armed is False
    assert window._speech.spoken == [], "todavía no está armada: no se anuncia"
    assert ("beep", {"name": "exit", "ms": 20000, "force": 1}) in window._manager.sent
    assert ("set", {"armed": 0, "buzzer": 1, "siren_ms": 120000}) in window._manager.sent
    assert window._hero._clock.isVisibleTo(window), "el reloj grande debe verse durante el retardo"
    assert window._hero._clock.text() == "00:20"

    now[0] = 19.2
    window._tick_delays()
    assert window._hero._state.text() == "ARMANDO"
    assert window._hero._clock.text() == "00:01", "se redondea hacia arriba"

    now[0] = 20.0
    window._tick_delays()
    assert window._hero._state.text().startswith("ARMADA")
    assert window._speech.spoken, "al vencer el retardo sí se anuncia"
    assert ("beep", {"name": "armed", "ms": 0, "force": 0}) in window._manager.sent


def test_desarmar_durante_el_retardo_de_salida_lo_cancela(window) -> None:
    window._manager = FakeManager()
    now = _reloj(window, exit_s=20)

    window.arm()
    window.disarm()

    assert window._hero._state.text() == "DESARMADA"
    assert ("beep", {"name": "stop"}) in window._manager.sent
    assert ("beep", {"name": "disarmed", "ms": 0, "force": 0}) in window._manager.sent

    now[0] = 60.0
    window._tick_delays()
    assert window._hero._state.text() == "DESARMADA", "el retardo cancelado no vuelve"
    assert not window._hero._clock.isVisibleTo(window), "sin retardo no hay reloj"
    assert not window._hero._clock_bar.isVisibleTo(window)


def test_el_reloj_solo_aparece_con_un_retardo_en_curso(window) -> None:
    """Fuera de los retardos el reloj no ocupa sitio en el hero."""
    assert not window._hero._clock.isVisibleTo(window)

    window._manager = FakeManager()
    now = _reloj(window, exit_s=90, entry_s=30)
    window.arm()
    assert window._hero._clock.text() == "01:30", "mas de un minuto se lee MM:SS"

    now[0] = 90.0
    window._tick_delays()
    assert window._hero._state.text().startswith("ARMADA")
    assert not window._hero._clock.isVisibleTo(window), "al armarse el reloj se retira"


def test_la_barra_del_reloj_mide_lo_que_queda(window) -> None:
    """La barra se vacia con el plazo, no se llena: es el margen restante."""
    window._manager = FakeManager()
    now = _reloj(window, exit_s=20)
    window.arm()
    assert window._hero._clock_bar.value() == 1000, "recien armado queda todo el plazo"

    now[0] = 15.0
    window._tick_delays()
    assert 240 < window._hero._clock_bar.value() < 260, "queda la cuarta parte"

    window.disarm()
    assert not window._hero._clock_bar.isVisibleTo(window)


def test_el_retardo_de_entrada_permite_desarmar(window) -> None:
    from alarm.protocol.messages import MotionEvent, MotionKind

    window._manager = FakeManager()
    now = _reloj(window, entry_s=15)
    window.arm()
    assert window._hero._state.text().startswith("ARMADA")

    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    assert window._hero._state.text() == "ENTRADA"
    assert window._hero._clock.text() == "00:15"
    assert ("beep", {"name": "entry", "ms": 15000, "force": 1}) in window._manager.sent
    assert window._controller.armed is True, "sigue vigilando mientras cuenta"

    now[0] = 10.0
    window._tick_delays()
    assert window._hero._clock.text() == "00:05"

    window.disarm()
    assert window._hero._state.text() == "DESARMADA"
    ahora = len(window._speech.spoken)
    now[0] = 100.0
    window._tick_delays()
    assert window._hero._state.text() == "DESARMADA"
    assert len(window._speech.spoken) == ahora, "no debe dispararse tras desarmar"


def test_el_retardo_de_entrada_dispara_al_vencer(window) -> None:
    from alarm.protocol.messages import MotionEvent, MotionKind

    window._manager = FakeManager()
    now = _reloj(window, entry_s=15)
    window.arm()
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    assert window._hero._state.text() == "ENTRADA"

    now[0] = 15.0
    window._tick_delays()

    assert window._hero._state.text() == "DISPARADA"
    assert ("beep", {"name": "alarm", "ms": 120000, "force": 0}) in window._manager.sent
    assert [cause for cause, _, _ in window._notifier.submitted] == ["armed", "triggered"]


def test_el_disparo_queda_en_el_registro_en_rojo(window) -> None:
    """Al reconstruir una intrusión, el disparo tiene que saltar a la vista."""
    from alarm.protocol.messages import MotionEvent, MotionKind
    from alarm.ui import theme

    window._manager = FakeManager()
    _reloj(window, entry_s=0)
    window._paint_alarm()
    window.arm()
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    assert window._hero._state.text() == "DISPARADA"

    disparos = [item for item in window._log._items if item.text(1) == "Alarma disparada"]
    assert len(disparos) == 1, "el disparo se registra una sola vez"
    fila = disparos[0]
    assert fila.foreground(0).color().name().upper() == theme.FAIL.upper()
    assert fila.font(0).bold(), "el disparo va en negrita, como los errores"
    assert "sirena 120 s" in fila.text(2)


def test_la_sirena_dura_lo_que_diga_la_configuracion(window) -> None:
    """El tiempo de pitido es configurable, no un valor fijo en el código."""
    from alarm.protocol.messages import MotionEvent, MotionKind

    window._manager = FakeManager()
    window._config = replace(window._config, buzzer=replace(window._config.buzzer, siren_ms=30_000))
    _reloj(window, entry_s=0)
    window._paint_alarm()
    window.arm()
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))

    assert ("beep", {"name": "alarm", "ms": 30000, "force": 0}) in window._manager.sent


def test_silenciar_corta_la_sirena_antes_de_su_tiempo(window) -> None:
    """Dos minutos son un tope, no una condena: silenciar la calla al instante."""
    from alarm.protocol.messages import MotionEvent, MotionKind

    window._manager = FakeManager()
    _reloj(window, entry_s=0)
    window._paint_alarm()
    window.arm()
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))

    window.silence()
    assert ("beep", {"name": "stop"}) in window._manager.sent
    assert window._hero._state.text() == "SILENCIADA"


def _reloj_rearme(
    window,
    *,
    siren_s: float = 5.0,
    rearm_s: int = 10,
    max_rearms: int = 2,
    start: float = 0.0,
) -> list[float]:
    """Controlador ya armado, con rearme automatico y reloj falso."""
    from alarm.core.state import AlarmController

    now = [start]
    window._config = replace(
        window._config,
        alarm=replace(
            window._config.alarm,
            auto_rearm=True,
            rearm_delay_s=rearm_s,
            max_rearms=max_rearms,
        ),
        buzzer=replace(window._config.buzzer, siren_ms=int(siren_s * 1000)),
    )
    window._controller = AlarmController(
        armed_on_start=True,
        auto_rearm=True,
        rearm_delay_s=rearm_s,
        max_rearms=max_rearms,
        siren_s=siren_s,
        clock=lambda: now[0],
    )
    window._paint_alarm()
    return now


def _fila(window, titulo: str):
    """Ultima fila del registro con ese titulo, o None."""
    filas = [item for item in window._log._items if item.text(1) == titulo]
    return filas[-1] if filas else None


def test_el_rearme_se_anuncia_en_el_disparo(window) -> None:
    from alarm.protocol.messages import MotionEvent, MotionKind

    window._manager = FakeManager()
    _reloj_rearme(window)
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))

    fila = _fila(window, "Alarma disparada")
    assert fila is not None
    assert "rearme 1 de 2 en 10 s" in fila.text(2), "el disparo dice qué va a pasar después"


def test_rearmando_muestra_el_reloj_y_la_fila(window) -> None:
    from alarm.protocol.messages import MotionEvent, MotionKind

    window._manager = FakeManager()
    now = _reloj_rearme(window)
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    assert window._hero._state.text() == "DISPARADA"

    now[0] = 5.0
    window._tick_delays()

    assert window._hero._state.text() == "REARMANDO"
    assert window._hero._clock.isVisibleTo(window), "la espera usa el reloj grande"
    assert window._hero._clock.text() == "00:10"
    assert ("beep", {"name": "stop"}) in window._manager.sent, "la sirena calla al vencer"

    fila = _fila(window, "Sirena terminada")
    assert fila is not None
    assert "rearme en 10 s (1 de 2)" in fila.text(2)


def test_el_rearme_devuelve_la_alarma_a_armada(window) -> None:
    from alarm.protocol.messages import MotionEvent, MotionKind

    window._manager = FakeManager()
    now = _reloj_rearme(window)
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    now[0] = 5.0
    window._tick_delays()
    assert window._hero._state.text() == "REARMANDO"

    ahora = len(window._speech.spoken)
    now[0] = 15.0
    window._tick_delays()

    assert window._hero._state.text().startswith("ARMADA")
    assert not window._hero._clock.isVisibleTo(window)
    assert ("beep", {"name": "armed", "ms": 0, "force": 0}) in window._manager.sent
    assert len(window._speech.spoken) == ahora + 1, "se anuncia el rearme por voz"
    assert "rearmado" in window._speech.spoken[-1].lower()

    fila = _fila(window, "Rearmada tras disparo")
    assert fila is not None
    assert "rearme 1 de 2" in fila.text(2)


def test_el_rearme_agotado_avisa_y_no_vuelve_a_armar(window) -> None:
    from alarm.protocol.messages import MotionEvent, MotionKind

    window._manager = FakeManager()
    now = _reloj_rearme(window, max_rearms=1)
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    now[0] = 5.0
    window._tick_delays()
    now[0] = 15.0
    window._tick_delays()
    assert window._hero._state.text().startswith("ARMADA")
    assert window._controller.rearm_count == 1

    window._on_message(MotionEvent(seq=2, kind=MotionKind.START, zone=1, ms=2000))
    now[0] = 20.0
    window._tick_delays()

    assert window._hero._state.text() == "DISPARADA"
    fila = _fila(window, "Rearme automático agotado")
    assert fila is not None
    assert "no volverá a armarse sola" in fila.text(2)
    assert "agotado" in window._status_label.text().lower()

    now[0] = 500.0
    window._tick_delays()
    assert window._hero._state.text() == "DISPARADA"


def test_desarmar_cancela_la_espera_del_rearme(window) -> None:
    from alarm.protocol.messages import MotionEvent, MotionKind

    window._manager = FakeManager()
    now = _reloj_rearme(window)
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    now[0] = 5.0
    window._tick_delays()
    assert window._hero._state.text() == "REARMANDO"

    window.disarm()
    assert window._hero._state.text() == "DESARMADA"
    assert window._controller.rearm_count == 0

    now[0] = 100.0
    window._tick_delays()
    assert window._hero._state.text() == "DESARMADA"


def test_el_hello_reanuda_la_cuenta_atras_en_la_placa(window) -> None:
    """Abrir el puerto reinicia el Arduino y se pierde el tono en curso."""
    from alarm.protocol.messages import Hello, Zone

    window._manager = FakeManager()
    now = _reloj(window, exit_s=20)
    window.arm()
    now[0] = 5.0
    window._manager.sent.clear()

    window._on_message(
        Hello(
            proto=1,
            dev="mega-pir",
            fw="1.4.1",
            boot_ms=0,
            zones=(Zone(1, "PIR", 2),),
            caps=(),
            cfg={"warmup_ms": 0},
        )
    )

    assert ("set", {"armed": 0, "buzzer": 1, "siren_ms": 120000}) in window._manager.sent
    assert (
        "beep",
        {"name": "exit", "ms": 15000, "force": 1},
    ) in window._manager.sent, "debe sonar solo lo que queda, no los 20 s"


def test_un_reinicio_de_la_placa_no_deja_la_sirena_en_silencio(window) -> None:
    """Abrir el puerto reinicia el Mega: si estaba disparada, vuelve a pitar."""
    from alarm.protocol.messages import Hello, MotionEvent, MotionKind, Zone

    window._manager = FakeManager()
    _reloj(window, entry_s=0)
    window._paint_alarm()
    window.arm()
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    window._manager.sent.clear()

    window._on_message(
        Hello(
            proto=1,
            dev="mega-pir",
            fw="1.5.0",
            boot_ms=0,
            zones=(Zone(1, "PIR", 2),),
            caps=(),
            cfg={"warmup_ms": 0},
        )
    )

    assert (
        "beep",
        {"name": "alarm", "ms": 120000, "force": 1},
    ) in window._manager.sent, "la sirena debe retomarse con toda su duracion"


def test_silenciada_la_placa_no_retoma_la_sirena_al_reconectar(window) -> None:
    """Quien ha silenciado no quiere que el USB vuelva a encender la sirena."""
    from alarm.protocol.messages import Hello, MotionEvent, MotionKind, Zone

    window._manager = FakeManager()
    _reloj(window, entry_s=0)
    window._paint_alarm()
    window.arm()
    window._on_message(MotionEvent(seq=1, kind=MotionKind.START, zone=1, ms=1000))
    window.silence()
    window._manager.sent.clear()

    window._on_message(
        Hello(
            proto=1,
            dev="mega-pir",
            fw="1.5.0",
            boot_ms=0,
            zones=(Zone(1, "PIR", 2),),
            caps=(),
            cfg={"warmup_ms": 0},
        )
    )

    assert not [
        payload
        for op, payload in window._manager.sent
        if op == "beep" and payload.get("name") == "alarm"
    ], "silenciada, no se vuelve a pedir la sirena"


def test_el_dialogo_de_retardos_guarda_y_prueba_la_cadencia(window) -> None:
    from alarm.ui.dialogs.delays import PREVIEW_MS, DelaysDialog

    window._manager = FakeManager()
    dialog = DelaysDialog(
        window,
        config=window._config,
        on_save=lambda config: None,
        on_preview=window._play_delay_preview,
        connected=True,
    )
    try:
        dialog._exit.setValue(70)
        dialog._entry.setValue(12)
        config = dialog.build_config()
        assert config.alarm.exit_delay_s == 70
        assert config.alarm.entry_delay_s == 12

        dialog._preview("entry")
        assert ("beep", {"name": "entry", "ms": PREVIEW_MS, "force": 1}) in window._manager.sent
        assert "entrada" in dialog._feedback.text()
    finally:
        dialog.deleteLater()


def test_el_dialogo_de_retardos_admite_cero_y_sin_conexion(window) -> None:
    from alarm.ui.dialogs.delays import DelaysDialog

    dialog = DelaysDialog(
        window,
        config=window._config,
        on_save=lambda config: None,
        on_preview=lambda kind, ms: True,
        connected=False,
    )
    try:
        dialog._exit.setValue(0)
        dialog._entry.setValue(0)
        config = dialog.build_config()
        assert config.alarm.exit_delay_s == 0
        assert config.alarm.entry_delay_s == 0
        assert dialog._exit_button.isEnabled() is False
    finally:
        dialog.deleteLater()


def test_el_dialogo_de_retardos_incluye_el_rearme(window) -> None:
    from alarm.ui.dialogs.delays import DelaysDialog

    dialog = DelaysDialog(
        window,
        config=window._config,
        on_save=lambda config: None,
        on_preview=lambda kind, ms: True,
        connected=True,
    )
    try:
        assert dialog._auto.isChecked() is False, "viene desactivado"
        assert dialog._rearm_min.value() == 5, "300 s se muestran como 5 min"
        assert dialog._rearm_max.value() == 3
        assert dialog._rearm_min.isEnabled() is False, "sin marcar la casilla no aplica"

        dialog._auto.setChecked(True)
        assert dialog._rearm_min.isEnabled() is True
        assert dialog._rearm_max.isEnabled() is True
        dialog._rearm_min.setValue(3)
        dialog._rearm_max.setValue(2)

        config = dialog.build_config()
        assert config.alarm.auto_rearm is True
        assert config.alarm.rearm_delay_s == 180, "el dialogo trabaja en minutos"
        assert config.alarm.max_rearms == 2
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# Mando remoto
# ---------------------------------------------------------------------------


class FakeRemoteListener:
    """Listener de mentira: recuerda las respuestas y no abre ningun socket."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, str, str, tuple[str, ...]]] = []
        self.running = True
        self.topic = "casa-abc123-mando"

    def reply(self, text: str, *, title: str, priority: str = "default", tags=()) -> bool:
        self.replies.append((title, text, priority, tuple(tags)))
        return True

    def stop(self) -> None:
        self.running = False

    def snapshot(self):
        from alarm.services.ntfy_commands import RemoteState

        return RemoteState(
            listening=self.running, connected=self.running, since=time.time(), topic=self.topic
        )

    def matches(self, *_args: object) -> bool:
        return True


def _remote_config(**overrides: object) -> AppConfig:
    from alarm.config import RemoteConfig

    base = default_config()
    ntfy = replace(
        base.ntfy,
        enabled=True,
        server="https://ntfy.sh",
        topic="casa-abc123",
    )
    remote = RemoteConfig(enabled=True, key="clave-de-prueba-1234")
    return replace(base, ntfy=ntfy, remote=replace(remote, **overrides))


def test_el_indicador_de_mando_empieza_apagado(window) -> None:
    assert window._config.remote.enabled is False
    tooltip = window._channels["mando"].toolTip()
    assert "desactivado" in tooltip
    assert window._remote_listener is None


def test_el_indicador_de_mando_avisa_si_falta_la_clave(window) -> None:
    window._apply_config(
        replace(
            _remote_config(),
            remote=replace(_remote_config().remote, enabled=True, key=""),
        )
    )
    assert "falta" in window._channels["mando"].toolTip()


def test_una_orden_remota_de_armar_arma_y_responde(window) -> None:
    from alarm.services.ntfy_commands import RemoteCommand

    listener = FakeRemoteListener()
    window._remote_listener = listener
    window._apply_config(_remote_config())

    window._on_remote_command(RemoteCommand(action="arm", text="armar"))

    from alarm.core.state import AlarmState

    assert window._controller.state in {AlarmState.ARMING, AlarmState.ARMED}
    assert listener.replies, "el movil debe recibir una respuesta"
    title, text, _priority, tags = listener.replies[-1]
    assert title == "[Mando] Armar"
    assert "Armada" in text
    assert "mando-reply" in tags


def test_una_orden_repetida_contesta_que_no_cambio_nada(window) -> None:
    from alarm.services.ntfy_commands import RemoteCommand

    listener = FakeRemoteListener()
    window._remote_listener = listener
    window._apply_config(_remote_config())
    window._controller.disarm()

    window._on_remote_command(RemoteCommand(action="disarm", text="desarmar"))

    title, text, _priority, _tags = listener.replies[-1]
    assert "sin cambios" in title
    assert "ya estaba desarmada" in text


def test_consultar_responde_con_el_estado(window) -> None:
    from alarm.services.ntfy_commands import RemoteCommand

    listener = FakeRemoteListener()
    window._remote_listener = listener
    window._apply_config(_remote_config())

    window._on_remote_command(RemoteCommand(action="status", text="consultar"))

    title, text, _priority, _tags = listener.replies[-1]
    assert title == "[Mando] Estado"
    assert "Estado:" in text
    assert "Enlace:" in text
    assert "Canales:" in text


def test_ayuda_no_revela_la_clave(window) -> None:
    from alarm.services.ntfy_commands import RemoteCommand

    listener = FakeRemoteListener()
    window._remote_listener = listener
    window._apply_config(_remote_config())

    window._on_remote_command(RemoteCommand(action="help", text="ayuda"))

    title, text, _priority, _tags = listener.replies[-1]
    assert title == "[Mando] Ayuda"
    assert "armar" in text and "desarmar" in text
    assert "clave-de-prueba-1234" not in text


def test_ayuda_responde_aunque_todo_lo_demas_este_desactivado(window) -> None:
    from alarm.services.ntfy_commands import RemoteCommand

    listener = FakeRemoteListener()
    window._remote_listener = listener
    window._apply_config(
        _remote_config(
            allow_arm=False,
            allow_disarm=False,
            allow_status=False,
            allow_silence=False,
            allow_panic=False,
        )
    )

    window._on_remote_command(RemoteCommand(action="help", text="ayuda"))

    title, text, _priority, _tags = listener.replies[-1]
    assert title == "[Mando] Ayuda"
    assert "armar" in text


def test_el_panico_dispara_y_no_lo_calla_el_enfriamiento(window) -> None:
    from alarm.services.ntfy_commands import RemoteCommand

    listener = FakeRemoteListener()
    window._remote_listener = listener
    window._apply_config(_remote_config(allow_panic=True))
    # Un disparo reciente ya agoto el enfriamiento.
    window._controller.allow("triggered")
    window._controller.allow("triggered")

    window._on_remote_command(RemoteCommand(action="panic", text="panico"))

    from alarm.core.state import AlarmState

    assert window._controller.state is AlarmState.TRIGGERED
    title, text, _priority, _tags = listener.replies[-1]
    assert "Pánico" in text or "sirena" in text.lower()


def test_una_orden_desactivada_no_se_ejecuta(window) -> None:
    from alarm.services.ntfy_commands import RemoteCommand

    listener = FakeRemoteListener()
    window._remote_listener = listener
    window._apply_config(_remote_config(allow_panic=False))

    window._on_remote_command(RemoteCommand(action="panic", text="panico"))

    from alarm.core.state import AlarmState

    assert window._controller.state is not AlarmState.TRIGGERED
    title, _text, _priority, tags = listener.replies[-1]
    assert title == "[Mando] No se pudo"
    assert "x" in tags


def test_el_dialogo_de_mando_valida_y_guarda(window) -> None:
    from alarm.services.ntfy_commands import RemoteState
    from alarm.ui.dialogs.remote import RemoteDialog

    guardados: list[tuple[AppConfig, dict[str, str]]] = []
    state = RemoteState(listening=False, connected=False, since=time.time(), topic="x")
    dialog = RemoteDialog(
        window,
        config=_remote_config(key=""),
        on_save=lambda config, secrets: guardados.append((config, secrets)),
        snapshot=lambda: state,
    )
    try:
        # Activar sin clave no debe guardar.
        dialog._key.setText("")
        dialog._totp_enabled.setChecked(False)
        dialog._enabled.setChecked(True)
        dialog.accept()
        assert guardados == []

        # Una clave corta tampoco.
        dialog._key.setText("corta")
        dialog.accept()
        assert guardados == []

        dialog._generate_key()
        assert len(dialog._key.text()) >= 12
        dialog.accept()
        assert len(guardados) == 1
        config, secrets = guardados[0]
        assert config.remote.enabled is True
        assert config.remote.key == dialog._key.text()
        assert secrets["ALARM_REMOTE_KEY"] == dialog._key.text()
        assert "ALARM_REMOTE_TOTP" not in secrets
    finally:
        dialog.deleteLater()


def test_el_dialogo_de_mando_genera_y_borra_el_totp(window) -> None:
    from alarm.services.ntfy_commands import RemoteState
    from alarm.ui.dialogs.remote import RemoteDialog

    guardados: list[tuple[AppConfig, dict[str, str]]] = []
    state = RemoteState(listening=False, connected=False, since=time.time(), topic="x")
    config = replace(
        _remote_config(),
        remote=replace(_remote_config().remote, totp_secret="JBSWY3DPEHPK3PXP"),
    )
    dialog = RemoteDialog(
        window,
        config=config,
        on_save=lambda cfg, secrets: guardados.append((cfg, secrets)),
        snapshot=lambda: state,
    )
    try:
        dialog._enabled.setChecked(True)
        assert dialog._totp_enabled.isChecked() is True
        dialog._generate_totp()
        dialog.accept()
        assert guardados, "con TOTP valido debe guardar"
        _config, secrets = guardados[0]
        assert secrets["ALARM_REMOTE_TOTP"]

        # Apagar TOTP borra el secreto del .env.
        dialog2 = RemoteDialog(
            window,
            config=config,
            on_save=lambda cfg, secrets: guardados.append((cfg, secrets)),
            snapshot=lambda: state,
        )
        dialog2._enabled.setChecked(True)
        dialog2._totp_enabled.setChecked(False)
        dialog2.accept()
        _config, secrets = guardados[-1]
        assert secrets["ALARM_REMOTE_TOTP"] == ""
        dialog2.deleteLater()
    finally:
        dialog.deleteLater()


def test_el_dialogo_de_mando_avisa_si_el_aviso_al_movil_esta_apagado(window) -> None:
    from alarm.services.ntfy_commands import RemoteState
    from alarm.ui.dialogs.remote import RemoteDialog

    guardados: list[tuple[AppConfig, dict[str, str]]] = []
    config = replace(_remote_config(), ntfy=replace(_remote_config().ntfy, enabled=False))
    dialog = RemoteDialog(
        window,
        config=config,
        on_save=lambda cfg, secrets: guardados.append((cfg, secrets)),
        snapshot=lambda: RemoteState(
            listening=False, connected=False, since=time.time(), topic="x"
        ),
    )
    try:
        dialog._enabled.setChecked(True)
        dialog.accept()
        assert guardados == [], "sin aviso al movil no se puede escuchar"
        assert "aviso al móvil" in dialog._status.text()
    finally:
        dialog.deleteLater()


def test_el_dialogo_de_mando_avisa_del_tema_publico(window) -> None:
    from alarm.services.ntfy_commands import RemoteState
    from alarm.ui.dialogs.remote import RemoteDialog

    snapshot = lambda: RemoteState(  # noqa: E731 - ayuda local de la prueba
        listening=False, connected=False, since=time.time(), topic="x"
    )
    # ntfy.sh + clave sin TOTP: el tema es publico y la clave se lee.
    dialog = RemoteDialog(
        window,
        config=_remote_config(key="clave-de-prueba-1234"),
        on_save=lambda cfg, secrets: None,
        snapshot=snapshot,
    )
    try:
        assert dialog._public_note.isVisibleTo(dialog) is True
        assert "ntfy.sh" in dialog._public_note.text()

        # Con TOTP el aviso desaparece: el codigo caduca en 30 s.
        dialog._totp_enabled.setChecked(True)
        dialog._generate_totp()
        assert dialog._public_note.isVisibleTo(dialog) is False
    finally:
        dialog.deleteLater()
