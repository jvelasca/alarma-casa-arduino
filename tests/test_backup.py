"""Copia completa: configuracion y secretos en un solo fichero.

Lo que se protege aqui es el viaje de ida y vuelta entre dos PCs: que salga todo
(secciones, puerto serie y credenciales), que vuelva identico, que un fichero
ajeno o roto se rechace antes de tocar nada, y que la restauracion no borre lo
que no viene en la copia.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from alarm.backup import (
    FORMAT,
    SECRET_KEYS,
    BackupError,
    backup_to_dict,
    build_backup,
    collect_secrets,
    export_backup,
    full_config_mapping,
    import_backup,
    read_backup,
    restore,
    write_backup,
)
from alarm.config import ConfigError, default_config, load_config, load_env
from alarm.settings_store import save_local

#: Secretos de mentira para no depender del `.env` real del repositorio: las
#: pruebas no deben leer ni escribir credenciales de verdad.
SECRETS = {
    "ALARM_SMTP_USER": "yo@gmail.com",
    "ALARM_SMTP_PASSWORD": "clave-de-aplicacion",
    "ALARM_TWILIO_SID": "AC123",
    "ALARM_TWILIO_TOKEN": "token-de-twilio",
    "ALARM_NTFY_TOKEN": "tk-de-ntfy",
}


def _config():
    base = default_config()
    return replace(
        base,
        email=replace(
            base.email,
            enabled=True,
            user="yo@gmail.com",
            password="clave-de-aplicacion",
            sender="yo@gmail.com",
            to=("casa@example.com",),
        ),
        sms=replace(base.sms, enabled=True, sid="AC123", token="token-de-twilio"),
        ntfy=replace(base.ntfy, enabled=True, topic="casa-abc123", token="tk-de-ntfy"),
        serial=replace(base.serial, port="COM4", baudrate=57600),
        log=replace(base.log, visible_rows=250, db_path="data/otro.db"),
    )


def test_la_copia_lleva_todas_las_secciones_y_el_puerto() -> None:
    backup = build_backup(
        _config(), secrets=SECRETS, machine="PC-ORIGEN", created="2026-11-12T20:00:00"
    )

    assert set(backup.sections) == {
        "alarm",
        "buzzer",
        "log",
        "notify",
        "remote",
        "serial",
        "tts",
        "ui",
    }
    assert backup.config["serial"]["port"] == "COM4"
    assert backup.config["serial"]["baudrate"] == 57600
    assert backup.config["log"]["visible_rows"] == 250
    assert backup.config["notify"]["email"]["to"] == ["casa@example.com"]
    assert backup.config["notify"]["ntfy"]["topic"] == "casa-abc123"


def test_los_secretos_no_se_mezclan_con_la_configuracion() -> None:
    """El TOML restaurado nunca debe contener la contraseña: va en `.env`."""
    backup = build_backup(_config(), secrets=SECRETS)

    texto = json.dumps(backup.config)
    assert "clave-de-aplicacion" not in texto
    assert "token-de-twilio" not in texto
    assert backup.secrets["ALARM_SMTP_PASSWORD"] == "clave-de-aplicacion"
    assert backup.secrets["ALARM_TWILIO_TOKEN"] == "token-de-twilio"


def test_exportar_sin_credenciales_deja_la_seccion_vacia() -> None:
    backup = build_backup(_config(), include_secrets=False)

    assert backup.secrets == {}
    assert backup.includes_secrets is False
    assert backup.secret_keys == ()


def test_collect_secrets_prefiere_el_env_y_completa_con_la_configuracion(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("ALARM_SMTP_USER=del-env@example.com\n", encoding="utf-8")

    valores = collect_secrets(_config(), env)

    assert valores["ALARM_SMTP_USER"] == "del-env@example.com"
    # Lo que no esta en `.env` sale de la configuracion ya cargada: puede venir
    # de una variable de entorno del sistema, y tambien hay que copiarlo.
    assert valores["ALARM_SMTP_PASSWORD"] == "clave-de-aplicacion"
    assert set(valores) <= set(SECRET_KEYS)


def test_ida_y_vuelta_por_fichero(tmp_path) -> None:
    original = build_backup(
        _config(), secrets=SECRETS, machine="PC-ORIGEN", created="2026-11-12T20:00:00"
    )
    destino = write_backup(original, tmp_path / "copia.alarmbackup")

    leida = read_backup(destino)

    assert leida.config == original.config
    assert leida.secrets == original.secrets
    assert leida.machine == "PC-ORIGEN"
    assert leida.created == "2026-11-12T20:00:00"
    assert leida.includes_secrets is True


def test_el_fichero_es_json_legible_con_su_marca(tmp_path) -> None:
    destino = write_backup(build_backup(_config(), secrets=SECRETS), tmp_path / "copia.alarmbackup")

    data = json.loads(destino.read_text(encoding="utf-8"))

    assert data["formato"] == FORMAT
    assert data["formato_version"] == 1
    assert data["incluye_credenciales"] is True
    assert data["secretos"]["ALARM_SMTP_PASSWORD"] == "clave-de-aplicacion"


def test_la_restauracion_escribe_la_capa_local_y_el_env(tmp_path) -> None:
    local = tmp_path / "config" / "alarm.local.toml"
    env = tmp_path / ".env"
    env.write_text("# mis notas\nALARM_TWILIO_TOKEN=viejo\n", encoding="utf-8")
    backup = build_backup(
        _config(), secrets=SECRETS, machine="PC-ORIGEN", created="2026-11-12T20:00:00"
    )

    resultado = restore(backup, local_path=local, env_path=env)

    assert resultado.local_path == local
    assert resultado.env_path == env
    assert len(resultado.secret_keys) == 5
    texto_local = local.read_text(encoding="utf-8")
    assert "restaurada desde una copia" in texto_local
    assert "clave-de-aplicacion" not in texto_local

    texto_env = env.read_text(encoding="utf-8")
    assert "# mis notas" in texto_env
    assert "ALARM_SMTP_PASSWORD=clave-de-aplicacion" in texto_env
    assert "ALARM_TWILIO_TOKEN=token-de-twilio" in texto_env
    assert "ALARM_NTFY_TOKEN=tk-de-ntfy" in texto_env


def test_lo_restaurado_se_carga_como_la_configuracion_de_origen(tmp_path) -> None:
    local = tmp_path / "alarm.local.toml"
    env = tmp_path / ".env"
    backup = build_backup(_config(), secrets=SECRETS)

    restore(backup, local_path=local, env_path=env)
    config = load_config(tmp_path / "no-existe.toml", local=local, env=load_env(env))

    assert config.serial.port == "COM4"
    assert config.serial.baudrate == 57600
    assert config.log.visible_rows == 250
    assert config.email.to == ("casa@example.com",)
    # Usuario y contraseña no viajan en el TOML: se recuperan de `.env`, que es
    # donde la aplicacion los lee al arrancar.
    assert config.email.user == "yo@gmail.com"
    assert config.email.password == "clave-de-aplicacion"
    assert config.sms.token == "token-de-twilio"


def test_restaurar_no_toca_los_secretos_que_no_vienen_en_la_copia(tmp_path) -> None:
    """Una copia sin credenciales no debe borrar las que ya hubiera."""
    env = tmp_path / ".env"
    env.write_text(
        "ALARM_SMTP_PASSWORD=la-de-este-pc\nALARM_OTRA_COSA=se queda\n", encoding="utf-8"
    )

    restore(
        build_backup(_config(), include_secrets=False),
        local_path=tmp_path / "l.toml",
        env_path=env,
    )

    texto = env.read_text(encoding="utf-8")
    assert texto == "ALARM_SMTP_PASSWORD=la-de-este-pc\nALARM_OTRA_COSA=se queda\n"


def test_una_copia_sin_secretos_devuelve_env_none(tmp_path) -> None:
    resultado = restore(
        build_backup(_config(), include_secrets=False),
        local_path=tmp_path / "l.toml",
        env_path=tmp_path / ".env",
    )

    assert resultado.env_path is None
    assert resultado.secret_keys == ()


def test_guardar_cualquier_ajuste_conserva_serial_y_log(tmp_path) -> None:
    """Un dialogo no puede borrar las secciones que no edita.

    La copia restaurada escribe `serial` y `log` en la capa local. Si guardar el
    volumen de voz reescribiera el fichero solo con sus claves, el puerto de la
    placa se perderia sin avisar.
    """
    local = tmp_path / "alarm.local.toml"
    restore(build_backup(_config(), secrets=SECRETS), local_path=local, env_path=tmp_path / ".env")

    save_local(replace(_config(), tts=replace(default_config().tts, volume=0.5)), local)

    texto = local.read_text(encoding="utf-8")
    assert "[serial]" in texto
    assert 'port = "COM4"' in texto
    assert "[log]" in texto
    assert "visible_rows = 250" in texto


def test_la_copia_completa_se_puede_exportar_en_un_paso(tmp_path) -> None:
    destino = export_backup(_config(), tmp_path / "copia.alarmbackup", secrets=SECRETS)

    assert read_backup(destino).secrets["ALARM_TWILIO_TOKEN"] == "token-de-twilio"


@pytest.mark.parametrize(
    ("contenido", "mensaje"),
    [
        ("no es json", "JSON inválido"),
        (json.dumps([1, 2, 3]), "no contiene un objeto"),
        (json.dumps({"formato": "otra-cosa"}), "no es una copia completa"),
        (
            json.dumps({"formato": FORMAT, "formato_version": 99, "config": {}}),
            "versión más nueva",
        ),
        (json.dumps({"formato": FORMAT, "config": "roto"}), "«config»"),
        (json.dumps({"formato": FORMAT, "config": {}, "secretos": 7}), "«secretos»"),
    ],
)
def test_una_copia_invalida_se_rechaza(tmp_path, contenido: str, mensaje: str) -> None:
    fichero = tmp_path / "mala.alarmbackup"
    fichero.write_text(contenido, encoding="utf-8")

    with pytest.raises(BackupError, match=mensaje):
        read_backup(fichero)


def test_una_configuracion_imposible_se_detecta_antes_de_escribir(tmp_path) -> None:
    fichero = tmp_path / "mala.alarmbackup"
    fichero.write_text(
        json.dumps(
            {
                "formato": FORMAT,
                "formato_version": 1,
                "config": {"alarm": {"cooldown_s": "mucho"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BackupError) as excinfo:
        read_backup(fichero)
    assert isinstance(excinfo.value.__cause__, ConfigError)
    assert not (tmp_path / "alarm.local.toml").exists()


def test_las_claves_de_secretos_desconocidas_se_descartan(tmp_path) -> None:
    fichero = tmp_path / "copia.alarmbackup"
    fichero.write_text(
        json.dumps(
            {
                "formato": FORMAT,
                "formato_version": 1,
                "config": full_config_mapping(default_config()),
                "secretos": {"ALARM_SMTP_PASSWORD": "x", "INVENTADA": "y"},
            }
        ),
        encoding="utf-8",
    )

    assert read_backup(fichero).secrets == {"ALARM_SMTP_PASSWORD": "x"}


def test_importar_desde_una_ruta(tmp_path) -> None:
    destino = write_backup(build_backup(_config(), secrets=SECRETS), tmp_path / "copia.alarmbackup")
    local = tmp_path / "restaurada.toml"

    resultado = import_backup(destino, local_path=local, env_path=tmp_path / ".env")

    assert resultado.sections == (
        "alarm",
        "buzzer",
        "log",
        "notify",
        "remote",
        "serial",
        "tts",
        "ui",
    )
    assert local.is_file()


def test_el_volcado_de_la_copia_serializa_todo() -> None:
    data = backup_to_dict(build_backup(_config(), include_secrets=False))

    assert data["equipo"]
    assert data["version"]
    assert data["config"]["serial"]["port"] == "COM4"
    assert data["secretos"] == {}
