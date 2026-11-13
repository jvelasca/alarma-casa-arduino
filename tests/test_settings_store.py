"""Pruebas del guardado de configuracion y de `.env`.

Lo importante aqui no es que se escriba un fichero, sino que:

- la configuracion sobreviva a la ida y vuelta (escribir y volver a leer da lo
  mismo), porque un serializador con un fallo sutil se descubre tarde;
- `.env` conserve comentarios y claves ajenas;
- una escritura interrumpida no deje un fichero a medias.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from alarm.config import AppConfig, default_config, load_config
from alarm.secrets import read_env_file, update_env
from alarm.settings_store import config_to_mapping, dump_toml, format_value, save_local


def test_escala_los_valores_a_toml() -> None:
    assert format_value(True) == "true"
    assert format_value(False) == "false"
    assert format_value(7) == "7"
    assert format_value(1.5) == "1.5"
    assert format_value('con "comillas"') == '"con \\"comillas\\""'
    assert format_value(["a", "b"]) == '["a", "b"]'


def test_ida_y_vuelta_de_la_configuracion(tmp_path: Path) -> None:
    base = default_config()
    events = dict(base.tts.events)
    events["triggered"] = replace(
        events["triggered"],
        text="Intruso a las {hora}",
        repeat=5,
        delay_ms=2500,
        gap_ms=1500,
        log=False,
    )
    config = replace(
        base,
        tts=replace(base.tts, events=events, voice="Voz de prueba", volume=42),
        ui=replace(base.ui, labels={**base.ui.labels, "armed": "EN GUARDIA"}),
        alarm=replace(
            base.alarm,
            exit_delay_s=75,
            entry_delay_s=20,
            auto_rearm=True,
            rearm_delay_s=600,
            max_rearms=4,
        ),
    )

    path = tmp_path / "alarm.local.toml"
    save_local(config, path)
    assert path.is_file()

    back = load_config(tmp_path / "no-base.toml", env={}, local=path)
    assert back.tts.voice == "Voz de prueba"
    assert back.tts.volume == 42
    assert back.tts.event("triggered").text == "Intruso a las {hora}"
    assert back.tts.event("triggered").repeat == 5
    assert back.tts.event("triggered").delay_ms == 2500
    assert back.tts.event("triggered").gap_ms == 1500
    assert back.tts.event("triggered").log is False
    assert back.ui.label("armed") == "EN GUARDIA"
    assert back.alarm.exit_delay_s == 75
    assert back.alarm.entry_delay_s == 20
    assert back.alarm.auto_rearm is True
    assert back.alarm.rearm_delay_s == 600
    assert back.alarm.max_rearms == 4
    assert back.email == config.email
    assert back.sms == config.sms
    assert back.ntfy == config.ntfy


def test_el_mando_remoto_da_la_vuelta_sin_secretos(tmp_path: Path) -> None:
    base = default_config()
    config = replace(
        base,
        remote=replace(
            base.remote,
            enabled=True,
            topic="mando-casa",
            allow_panic=True,
            reply=False,
            lockout_failures=3,
            lockout_minutes=5,
            key="clave-secreta-123456",
            totp_secret="JBSWY3DPEHPK3PXP",
        ),
    )
    path = tmp_path / "alarm.local.toml"
    save_local(config, path)
    text = path.read_text(encoding="utf-8")
    assert "clave-secreta-123456" not in text
    assert "JBSWY3DPEHPK3PXP" not in text

    back = load_config(tmp_path / "no-base.toml", env={}, local=path)
    assert back.remote.enabled is True
    assert back.remote.topic == "mando-casa"
    assert back.remote.allow_panic is True
    assert back.remote.reply is False
    assert back.remote.lockout_failures == 3
    assert back.remote.lockout_minutes == 5
    # Los secretos solo vuelven por `.env`.
    assert back.remote.key == ""
    assert back.remote.totp_secret == ""


def test_la_capa_guardada_no_incluye_secretos(tmp_path: Path) -> None:
    base = default_config()
    config = replace(
        base,
        email=replace(base.email, user="cuenta@example.com", password="super-secreta"),
        sms=replace(base.sms, sid="AC123", token="token-secreto"),
        ntfy=replace(base.ntfy, topic="casa-abc123", token="tk-ntfy-secreto"),
    )
    path = tmp_path / "alarm.local.toml"
    save_local(config, path)
    text = path.read_text(encoding="utf-8")
    assert "super-secreta" not in text
    assert "token-secreto" not in text
    assert "tk-ntfy-secreto" not in text
    assert "casa-abc123" in text, "el tema no es un secreto: se guarda con la configuracion"
    assert "AC123" not in text


def test_el_fichero_guardado_lleva_cabecera_explicativa() -> None:
    text = dump_toml(config_to_mapping(default_config()))
    assert text.startswith("# Configuracion escrita desde la aplicacion.")
    assert "[tts.events.armed]" in text
    assert "[notify.sms]" in text
    assert "[notify.ntfy]" in text


def test_actualizar_env_conserva_lo_demas(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# mis notas\nALARM_SMTP_USER=viejo@example.com\nOTRA_CLAVE=no-tocar\n",
        encoding="utf-8",
    )
    update_env(path, {"ALARM_SMTP_USER": "nuevo@example.com"})
    text = path.read_text(encoding="utf-8")
    assert "# mis notas" in text
    assert "OTRA_CLAVE=no-tocar" in text
    assert "ALARM_SMTP_USER=nuevo@example.com" in text
    assert "viejo@example.com" not in text


def test_actualizar_env_anade_las_que_faltan(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("OTRA=1\n", encoding="utf-8")
    update_env(
        path,
        {"ALARM_TWILIO_SID": "AC999", "ALARM_TWILIO_TOKEN": "con espacios"},
    )
    values = read_env_file(path)
    assert values["OTRA"] == "1"
    assert values["ALARM_TWILIO_SID"] == "AC999"
    assert values["ALARM_TWILIO_TOKEN"] == "con espacios"


def test_actualizar_env_crea_el_fichero(tmp_path: Path) -> None:
    path = tmp_path / "sub" / ".env"
    update_env(path, {"ALARM_SMTP_PASSWORD": "secreta"})
    assert read_env_file(path)["ALARM_SMTP_PASSWORD"] == "secreta"


def test_guardar_es_repetible_sin_duplicar_tablas(tmp_path: Path) -> None:
    config: AppConfig = default_config()
    path = tmp_path / "alarm.local.toml"
    save_local(config, path)
    primera = path.read_text(encoding="utf-8")
    save_local(config, path)
    segunda = path.read_text(encoding="utf-8")
    assert primera == segunda
    assert segunda.count("[tts]") == 1


def test_la_capa_local_guarda_la_casilla_sin_sensor(tmp_path: Path) -> None:
    base = default_config()
    config = replace(
        base,
        email=replace(base.email, on_device_lost=False),
        sms=replace(base.sms, on_device_lost=False),
    )
    path = tmp_path / "alarm.local.toml"
    save_local(config, path)

    back = load_config(tmp_path / "no-base.toml", env={}, local=path)
    assert back.email.on_device_lost is False
    assert back.sms.on_device_lost is False


def test_la_proyeccion_incluye_la_casilla_sin_sensor() -> None:
    mapping = config_to_mapping(default_config())
    assert mapping["notify"]["email"]["on_device_lost"] is True
    assert mapping["notify"]["sms"]["on_device_lost"] is True
