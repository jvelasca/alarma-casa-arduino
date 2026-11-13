"""Pruebas de la configuracion: defaults, TOML, entorno y validacion."""

from __future__ import annotations

from pathlib import Path

import pytest

from alarm.config import (
    ConfigError,
    apply_env,
    default_config,
    from_mapping,
    load_config,
    parse_env,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "alarm.example.toml"


def test_valores_por_defecto_sin_fichero(tmp_path: Path) -> None:
    config = load_config(tmp_path / "no-existe.toml", env={}, local=tmp_path / "no-local.toml")
    assert config.tts.enabled is True
    assert config.tts.repeat == 2
    assert config.email.enabled is False
    assert config.buzzer.pin == 11
    assert "triggered" in config.tts.messages


def test_los_retardos_por_defecto_son_los_de_una_central() -> None:
    config = default_config()
    assert config.alarm.exit_delay_s == 45
    assert config.alarm.entry_delay_s == 30
    assert config.ui.label("arming") == "ARMANDO"
    assert config.ui.label("pending") == "ENTRADA"


def test_el_estado_operativo_se_restaura_por_defecto() -> None:
    """Un reinicio no puede desarmar la casa sin que nadie lo pida."""
    config = default_config()
    assert config.alarm.armed_on_start is False
    assert config.alarm.restore_state_on_start is True

    apagado = from_mapping({"alarm": {"restore_state_on_start": False}})
    assert apagado.alarm.restore_state_on_start is False


def test_los_retardos_se_ajustan_y_se_acotan() -> None:
    config = from_mapping({"alarm": {"exit_delay_s": 90, "entry_delay_s": 0}})
    assert config.alarm.exit_delay_s == 90
    assert config.alarm.entry_delay_s == 0, "0 desactiva el retardo"

    with pytest.raises(ConfigError, match="alarm.exit_delay_s"):
        from_mapping({"alarm": {"exit_delay_s": 301}})
    with pytest.raises(ConfigError, match="alarm.entry_delay_s"):
        from_mapping({"alarm": {"entry_delay_s": -1}})


def test_el_rearme_automatico_viene_desactivado() -> None:
    config = default_config()
    assert config.alarm.auto_rearm is False
    assert config.alarm.rearm_delay_s == 300, "cinco minutos"
    assert config.alarm.max_rearms == 3
    assert config.ui.label("rearming") == "REARMANDO"


def test_el_rearme_se_ajusta_y_se_acota() -> None:
    config = from_mapping({"alarm": {"auto_rearm": True, "rearm_delay_s": 900, "max_rearms": 5}})
    assert config.alarm.auto_rearm is True
    assert config.alarm.rearm_delay_s == 900
    assert config.alarm.max_rearms == 5

    with pytest.raises(ConfigError, match="alarm.max_rearms"):
        from_mapping({"alarm": {"max_rearms": 0}})
    with pytest.raises(ConfigError, match="alarm.rearm_delay_s"):
        from_mapping({"alarm": {"rearm_delay_s": -1}})


def test_el_texto_de_voz_del_rearme_admite_el_numero() -> None:
    config = default_config()
    texto = config.tts.text("rearmed", n=2, max=3, hora="10:00", evento="x", zona="z")
    assert "2" in texto and "3" in texto


def test_el_ejemplo_del_repositorio_es_valido(tmp_path: Path) -> None:
    config = load_config(EXAMPLE, env={}, local=tmp_path / "no-local.toml")
    assert config.email.port == 587
    assert config.email.to == ("yo@example.com",)
    assert config.email.security == "starttls"
    assert config.buzzer.siren_ms == 120_000, "dos minutos: una sirena de 15 s no da tiempo"


def test_el_ejemplo_trae_los_eventos_de_voz(tmp_path: Path) -> None:
    config = load_config(EXAMPLE, env={}, local=tmp_path / "no-local.toml")
    armed = config.tts.event("armed")
    assert armed.text == "Alarma armada."
    assert armed.label == "ARMADA"
    assert armed.repeat == 2
    assert armed.gap_ms == 0
    assert armed.delay_ms == 0
    assert armed.log is True

    triggered = config.tts.event("triggered")
    assert "{hora}" in triggered.text
    assert config.ui.label("triggered") == "DISPARADA"


def test_el_ejemplo_trae_el_bloque_sms(tmp_path: Path) -> None:
    config = load_config(EXAMPLE, env={}, local=tmp_path / "no-local.toml")
    assert config.sms.provider == "twilio"
    assert config.sms.to == ("+34600000000",)
    assert config.sms.on_triggered is True
    assert config.sms.on_armed is False
    assert config.sms.enabled is False
    assert config.sms.configured is False, "sin SID ni token no esta configurado"


def test_el_ejemplo_trae_el_bloque_ntfy(tmp_path: Path) -> None:
    config = load_config(EXAMPLE, env={}, local=tmp_path / "no-local.toml")
    assert config.ntfy.server == "https://ntfy.sh"
    assert config.ntfy.enabled is False
    assert config.ntfy.on_triggered is True
    assert config.ntfy.on_armed is False
    assert config.ntfy.priority_triggered == "urgent"
    assert config.ntfy.priority_device_lost == "high"
    assert config.ntfy.configured is False, "sin tema no esta configurado"


def test_el_bloque_ntfy_es_configurable() -> None:
    config = from_mapping(
        {
            "notify": {
                "ntfy": {
                    "enabled": True,
                    "server": "ntfy.midominio.com",
                    "topic": "casa-abc123",
                    "priority_triggered": "high",
                    "on_armed": True,
                }
            }
        }
    )
    assert config.ntfy.enabled is True
    assert config.ntfy.base_url == "https://ntfy.midominio.com"
    assert config.ntfy.topic == "casa-abc123"
    assert config.ntfy.priority_triggered == "high"
    assert config.ntfy.priority_device_lost == "high"
    assert config.ntfy.on_armed is True
    assert config.ntfy.configured is True


def test_un_tema_de_ntfy_invalido_falla() -> None:
    with pytest.raises(ConfigError, match="notify.ntfy.topic"):
        from_mapping({"notify": {"ntfy": {"topic": "con espacio"}}})


def test_un_servidor_de_ntfy_invalido_falla() -> None:
    with pytest.raises(ConfigError, match="notify.ntfy.server"):
        from_mapping({"notify": {"ntfy": {"server": "ftp://ntfy.sh"}}})


def test_una_prioridad_de_ntfy_desconocida_falla() -> None:
    with pytest.raises(ConfigError, match="notify.ntfy.priority_triggered"):
        from_mapping({"notify": {"ntfy": {"priority_triggered": "altisima"}}})


def test_el_token_de_ntfy_viene_del_entorno() -> None:
    config = apply_env(default_config(), {"ALARM_NTFY_TOKEN": "tk_123"})
    assert config.ntfy.token == "tk_123"


def test_los_eventos_se_pueden_definir_por_tabla() -> None:
    config = from_mapping(
        {
            "tts": {
                "events": {
                    "armed": {"text": "Casa protegida", "label": "PROTEGIDA", "repeat": 4},
                    "disarmed": {"enabled": False, "delay_ms": 1500},
                    "triggered": {"gap_ms": 2000, "log": False},
                }
            }
        }
    )
    assert config.tts.event("armed").text == "Casa protegida"
    assert config.tts.event("armed").label == "PROTEGIDA"
    assert config.tts.event("armed").repeat == 4
    assert config.tts.event("disarmed").enabled is False
    assert config.tts.event("disarmed").delay_ms == 1500
    assert config.tts.event("disarmed").text == "Alarma desarmada."
    assert config.tts.event("triggered").gap_ms == 2000
    assert config.tts.event("triggered").log is False


def test_un_intervalo_fuera_de_rango_falla() -> None:
    with pytest.raises(ConfigError, match=r"tts\.events\.armed\.gap_ms"):
        from_mapping({"tts": {"events": {"armed": {"gap_ms": 999_999}}}})


def test_migracion_desde_tts_messages() -> None:
    config = from_mapping({"tts": {"messages": {"armed": "Modo guardia"}}})
    assert config.tts.event("armed").text == "Modo guardia"
    assert config.tts.text("armed") == "Modo guardia"


def test_proveedor_sms_desconocido_falla() -> None:
    with pytest.raises(ConfigError, match="notify.sms.provider"):
        from_mapping({"notify": {"sms": {"provider": "paloma"}}})


def test_las_etiquetas_de_estado_son_configurables() -> None:
    config = from_mapping({"ui": {"labels": {"armed": "EN GUARDIA"}}})
    assert config.ui.label("armed") == "EN GUARDIA"
    assert config.ui.label("disarmed") == "DESARMADA"


def test_la_capa_local_gana_sobre_la_base(tmp_path: Path) -> None:
    base = tmp_path / "alarm.toml"
    base.write_text(
        '[tts]\nvoice = "Voz base"\nvolume = 50\n\n[ui.labels]\narmed = "BASE"\n',
        encoding="utf-8",
    )
    local = tmp_path / "alarm.local.toml"
    local.write_text(
        '[tts]\nvolume = 90\n\n[ui.labels]\narmed = "LOCAL"\n',
        encoding="utf-8",
    )
    config = load_config(base, env={}, local=local)
    assert config.tts.voice == "Voz base", "lo que no esta en la capa local se hereda"
    assert config.tts.volume == 90
    assert config.ui.label("armed") == "LOCAL"
    assert "alarm.local.toml" in config.source


def test_sin_ficheros_no_falla(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nada.toml", env={}, local=tmp_path / "tampoco.toml")
    assert config.source == "valores por defecto"
    assert config.tts.event("armed").text == "Alarma armada."


def test_las_credenciales_de_twilio_vienen_del_entorno() -> None:
    config = apply_env(
        default_config(),
        {"ALARM_TWILIO_SID": "AC123", "ALARM_TWILIO_TOKEN": "token"},
    )
    assert config.sms.sid == "AC123"
    assert config.sms.token == "token"


def test_las_credenciales_vienen_del_entorno() -> None:
    base = default_config()
    config = apply_env(
        base,
        {"ALARM_SMTP_USER": "cuenta@gmail.com", "ALARM_SMTP_PASSWORD": "secreto"},
    )
    assert config.email.user == "cuenta@gmail.com"
    assert config.email.password == "secreto"


def test_parse_env_admite_comentarios_y_comillas() -> None:
    values = parse_env(
        "\n".join(
            [
                "# comentario",
                "",
                "export ALARM_SMTP_USER='a@b.c'",
                'ALARM_SMTP_PASSWORD="con espacios"',
                "SIN_VALOR",
            ]
        )
    )
    assert values == {
        "ALARM_SMTP_USER": "a@b.c",
        "ALARM_SMTP_PASSWORD": "con espacios",
    }


def test_toml_invalido_falla_con_mensaje_claro(tmp_path: Path) -> None:
    bad = tmp_path / "alarm.toml"
    bad.write_text("[tts\nenabled = true", encoding="utf-8")
    with pytest.raises(ConfigError, match="TOML invalido"):
        load_config(bad, env={})


def test_volumen_fuera_de_rango() -> None:
    with pytest.raises(ConfigError, match="tts.volume"):
        from_mapping({"tts": {"volume": 200}})


def test_seguridad_desconocida() -> None:
    with pytest.raises(ConfigError, match="security"):
        from_mapping({"notify": {"email": {"security": "inventada"}}})


def test_plantilla_desconocida_no_rompe_el_aviso() -> None:
    config = default_config()
    assert config.tts.text("inexistente") == "inexistente"
    # Sin el valor de `hora`, la sustitucion falla y se devuelve la plantilla
    # intacta en vez de lanzar una excepcion dentro del hilo de voz.
    assert "{hora}" in config.tts.text("triggered")


# ---------------------------------------------------------------------------
# Conexion permanente e historial
# ---------------------------------------------------------------------------


def test_por_defecto_la_placa_se_conecta_sola_y_se_supervisa() -> None:
    config = default_config()
    assert config.serial.auto_connect is True
    assert config.serial.heartbeat_timeout_s == 8.0
    assert config.serial.reconnect_max_s == 30.0
    assert config.serial.handshake_timeout_s == 3.0


def test_el_bloque_serial_admite_la_politica_de_reconexion() -> None:
    config = from_mapping(
        {
            "serial": {
                "port": "COM5",
                "auto_connect": False,
                "reconnect_max_s": 12.5,
                "heartbeat_timeout_s": 4.0,
                "handshake_timeout_s": 6.0,
            }
        }
    )
    assert config.serial.port == "COM5"
    assert config.serial.auto_connect is False
    assert config.serial.reconnect_max_s == 12.5
    assert config.serial.heartbeat_timeout_s == 4.0
    assert config.serial.handshake_timeout_s == 6.0


@pytest.mark.parametrize(
    ("tabla", "clave"),
    [
        ({"serial": {"heartbeat_timeout_s": 0.5}}, "serial.heartbeat_timeout_s"),
        ({"serial": {"reconnect_max_s": 0}}, "serial.reconnect_max_s"),
        ({"serial": {"handshake_timeout_s": 0.2}}, "serial.handshake_timeout_s"),
        ({"serial": {"handshake_timeout_s": 60}}, "serial.handshake_timeout_s"),
        ({"log": {"max_events": 0}}, "log.max_events"),
        ({"log": {"visible_rows": 0}}, "log.visible_rows"),
        ({"log": {"max_age_days": 0}}, "log.max_age_days"),
    ],
)
def test_valores_de_conexion_fuera_de_rango(tabla: dict, clave: str) -> None:
    with pytest.raises(ConfigError, match=clave):
        from_mapping(tabla)


def test_por_defecto_el_historial_se_restringe_a_un_ano_y_cien_mil() -> None:
    log = default_config().log
    assert log.max_events == 100_000
    assert log.max_age_days == 365
    assert log.visible_rows == 1_000
    assert log.db_path == "data/alarm.db"


def test_el_bloque_log_es_configurable() -> None:
    config = from_mapping(
        {"log": {"max_events": 5000, "max_age_days": 30, "visible_rows": 500, "db_path": "x.db"}}
    )
    assert config.log.max_events == 5000
    assert config.log.max_age_days == 30
    assert config.log.visible_rows == 500
    assert config.log.db_path == "x.db"


def test_la_perdida_de_enlace_avisa_por_correo_y_sms_por_defecto() -> None:
    config = default_config()
    assert config.email.on_device_lost is True
    assert config.sms.on_device_lost is True
    assert config.email.enabled_for("device_lost") is True
    assert config.sms.enabled_for("device_lost") is True
    # Las causas desconocidas no generan aviso: mejor callar que inventar.
    assert config.email.enabled_for("inventada") is False


def test_la_casilla_sin_sensor_se_puede_desactivar() -> None:
    config = from_mapping(
        {"notify": {"email": {"on_device_lost": False}, "sms": {"on_device_lost": False}}}
    )
    assert config.email.enabled_for("device_lost") is False
    assert config.sms.enabled_for("device_lost") is False
    assert config.email.enabled_for("triggered") is True


def test_el_ejemplo_documenta_la_conexion_y_el_historial(tmp_path: Path) -> None:
    config = load_config(EXAMPLE, env={}, local=tmp_path / "no-local.toml")
    assert config.serial.auto_connect is True
    assert config.serial.heartbeat_timeout_s == 8.0
    assert config.serial.handshake_timeout_s == 3.0
    assert config.log.max_events == 100_000
    assert config.log.max_age_days == 365
    assert config.email.on_device_lost is True
    assert config.sms.on_device_lost is True
    assert "desconectado" in config.tts.text("device_lost")
    assert config.ui.label("device_lost") == "SIN SENSOR"


# ---------------------------------------------------------------------------
# Mando remoto
# ---------------------------------------------------------------------------


def test_el_mando_remoto_viene_desactivado_y_sin_panico() -> None:
    remote = default_config().remote
    assert remote.enabled is False
    assert remote.allow_arm is True
    assert remote.allow_disarm is True
    assert remote.allow_status is True
    assert remote.allow_silence is True
    assert remote.allow_panic is False, "la sirena remota no se regala"
    assert remote.reply is True
    assert remote.lockout_failures == 5
    assert remote.lockout_minutes == 15


def test_el_tema_de_mando_se_deriva_del_de_avisos() -> None:
    config = from_mapping({"notify": {"ntfy": {"topic": "casa-abc123"}}})
    assert config.remote_topic == "casa-abc123-mando"
    otro = from_mapping(
        {"notify": {"ntfy": {"topic": "casa-abc123"}}, "remote": {"topic": "mando-casa"}}
    )
    assert otro.remote_topic == "mando-casa"


def test_el_mando_sin_avisos_dice_que_falta() -> None:
    config = from_mapping({"remote": {"enabled": True}})
    missing = config.remote_missing()
    assert "datos del móvil (servidor y tema)" in missing
    assert "clave de mando o código TOTP" in missing
    # Con todo puesto, no falta nada.
    completo = from_mapping(
        {"notify": {"ntfy": {"topic": "casa-abc123"}}, "remote": {"enabled": True}}
    )
    completo = apply_env(completo, {"ALARM_REMOTE_KEY": "clave-de-prueba-1234"})
    assert completo.remote_missing() == ()


def test_el_bloque_remote_limita_los_intentos_y_el_bloqueo() -> None:
    config = from_mapping(
        {"remote": {"lockout_failures": 0, "lockout_minutes": 1, "allow_panic": True}}
    )
    assert config.remote.lockout_failures == 0, "0 desactiva el bloqueo"
    assert config.remote.allow_panic is True
    assert "panic" in config.remote.allows

    for key, value in (("lockout_failures", 51), ("lockout_minutes", 0)):
        with pytest.raises(ConfigError, match=f"remote.{key}"):
            from_mapping({"remote": {key: value}})


def test_un_tema_de_mando_invalido_falla() -> None:
    with pytest.raises(ConfigError, match="remote.topic"):
        from_mapping({"remote": {"topic": "con espacio"}})
    with pytest.raises(ConfigError, match="64 caracteres"):
        from_mapping(
            {
                "notify": {"ntfy": {"topic": "x" * 60}},
                "remote": {"enabled": True},
            }
        )


def test_la_clave_y_el_totp_vienen_del_entorno() -> None:
    config = apply_env(
        default_config(),
        {
            "ALARM_REMOTE_KEY": "clave-de-prueba-1234",
            "ALARM_REMOTE_TOTP": "JBSWY3DPEHPK3PXP",
        },
    )
    assert config.remote.key == "clave-de-prueba-1234"
    assert config.remote.totp_secret == "JBSWY3DPEHPK3PXP"
    assert config.remote.authenticates is True
    assert config.remote.uses_totp is True


def test_el_ejemplo_trae_el_bloque_remote(tmp_path: Path) -> None:
    config = load_config(EXAMPLE, env={}, local=tmp_path / "no-local.toml")
    assert config.remote.enabled is False
    assert config.remote.allow_panic is False
    assert config.remote.lockout_failures == 5


def test_el_login_por_clave_sin_totp_no_toca_el_tema(tmp_path: Path) -> None:
    """La clave vive en `.env`: el TOML de ejemplo no la menciona."""
    texto = EXAMPLE.read_text(encoding="utf-8")
    assert "ALARM_REMOTE_KEY" in texto
    assert "clave-de-prueba" not in texto
