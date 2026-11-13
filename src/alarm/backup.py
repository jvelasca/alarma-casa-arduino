"""Copia de seguridad completa de la configuracion, credenciales incluidas.

Para llevar la alarma a otro PC hacen falta tres cosas que hoy viven en sitios
distintos: los ajustes (`alarm.toml` + `alarm.local.toml`), los secretos
(contrasena de aplicacion de Gmail, token de Twilio) en `.env`, y el criterio de
que puerto serie usar. Este modulo los junta en **un solo fichero** que se puede
copiar en un pendrive, y lo sabe leer de vuelta.

El fichero es JSON legible aunque lleve la extension `.alarmbackup`: si algo va
mal, se puede abrir con el Bloc de notas y ver que hay dentro. **No va cifrado**,
porque la aplicacion no trae dependencias de criptografia y una cifra casera
seria peor que no cifrar. Por eso:

- el nombre y el aviso de la interfaz dicen claramente que contiene secretos;
- se escribe con permisos de solo el usuario (`0600` en sistemas que los usan);
- la copia sin credenciales sigue existiendo (`include_secrets=False`) para
  adjuntar a un informe o a una consulta de soporte.

La restauracion no toca `alarm.toml` (el fichero base con comentarios): escribe
la capa `alarm.local.toml`, que es la que la aplicacion lee por encima. Asi el
ejemplo sigue intacto y se puede volver a empezar borrando solo la capa local.

El historial de eventos (`data/alarm.db`) **no** viaja en la copia: es un
volumen aparte, se exporta a CSV desde la ventana (`Archivo → Exportar registro`)
y no forma parte de «como esta configurada la alarma».
"""

from __future__ import annotations

import json
import logging
import os
import platform
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    ENV_FILE,
    LOCAL_FILE,
    AppConfig,
    ConfigError,
    from_mapping,
    read_toml,
)
from .secrets import read_env_file, update_env
from .settings_store import config_to_mapping, dump_toml, write_atomic

log = logging.getLogger(__name__)

#: Marca que distingue una copia completa de un JSON cualquiera.
FORMAT = "alarma-casa-copia"

#: Sube cuando cambie el significado de algun campo, no al anadir uno nuevo.
FORMAT_VERSION = 1

#: Extension propia: evita que la copia se confunda con el volcado sin secretos.
EXTENSION = ".alarmbackup"

#: Claves de `.env` que la aplicacion escribe. Se copian todas las que tengan
#: valor; las que falten no se inventan.
SECRET_KEYS: tuple[str, ...] = (
    "ALARM_SMTP_USER",
    "ALARM_SMTP_PASSWORD",
    "ALARM_TWILIO_SID",
    "ALARM_TWILIO_TOKEN",
    "ALARM_NTFY_TOKEN",
    "ALARM_REMOTE_KEY",
    "ALARM_REMOTE_TOTP",
)

HEADER = """\
# Configuracion restaurada desde una copia de seguridad.
#
# Se superpone a `config/alarm.toml`. Los secretos (contrasena SMTP, tokens de
# Twilio y ntfy, clave y TOTP del mando remoto) estan en `.env`, no aqui. Si
# quieres empezar de cero, borra este fichero y la aplicacion vuelve a los
# valores del ejemplo.
"""


class BackupError(ValueError):
    """La copia no se puede leer, no es de este programa o trae datos invalidos."""


@dataclass(frozen=True, slots=True)
class Backup:
    """Contenido de una copia, ya validado."""

    created: str
    version: str
    machine: str
    includes_secrets: bool
    config: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def sections(self) -> tuple[str, ...]:
        return tuple(sorted(self.config))

    @property
    def secret_keys(self) -> tuple[str, ...]:
        return tuple(key for key in SECRET_KEYS if key in self.secrets)


@dataclass(frozen=True, slots=True)
class Restored:
    """Resultado de aplicar una copia: rutas escritas y claves actualizadas."""

    local_path: Path
    env_path: Path | None
    secret_keys: tuple[str, ...]
    sections: tuple[str, ...]


# ---------------------------------------------------------------------------
# Construccion
# ---------------------------------------------------------------------------


def collect_secrets(config: AppConfig, env_path: Path | None = None) -> dict[str, str]:
    """Secretos efectivos, del `.env` o del entorno del proceso.

    `load_config` ya ha volcado `.env` sobre la configuracion, asi que
    `config.email.password` tiene el valor que se esta usando aunque venga de una
    variable de entorno del sistema. Se prefiere lo que haya en `.env` y se
    rellena lo que falte con lo que tenga la configuracion en memoria.
    """
    stored = read_env_file(env_path)
    from_config = {
        "ALARM_SMTP_USER": config.email.user,
        "ALARM_SMTP_PASSWORD": config.email.password,
        "ALARM_TWILIO_SID": config.sms.sid,
        "ALARM_TWILIO_TOKEN": config.sms.token,
        "ALARM_NTFY_TOKEN": config.ntfy.token,
        "ALARM_REMOTE_KEY": config.remote.key,
        "ALARM_REMOTE_TOTP": config.remote.totp_secret,
    }
    values: dict[str, str] = {}
    for key in SECRET_KEYS:
        value = stored.get(key) or from_config[key]
        if value:
            values[key] = value
    return values


def full_config_mapping(config: AppConfig) -> dict[str, Any]:
    """Configuracion completa en tipos TOML, incluidas las secciones de solo lectura.

    `config_to_mapping` cubre lo que los dialogos editan; aqui se anaden las dos
    secciones que solo se tocan a mano (`serial` y `log`), porque «la misma
    configuracion» incluye el puerto de la placa y la retencion del historial.
    """
    data = config_to_mapping(config)
    data["serial"] = {
        "port": config.serial.port,
        "baudrate": config.serial.baudrate,
        "auto_connect": config.serial.auto_connect,
        "reconnect_max_s": config.serial.reconnect_max_s,
        "heartbeat_timeout_s": config.serial.heartbeat_timeout_s,
    }
    data["log"] = {
        "max_events": config.log.max_events,
        "max_age_days": config.log.max_age_days,
        "visible_rows": config.log.visible_rows,
        "db_path": config.log.db_path,
    }
    return data


def build_backup(
    config: AppConfig,
    *,
    secrets: Mapping[str, str] | None = None,
    include_secrets: bool = True,
    env_path: Path | None = None,
    machine: str | None = None,
    created: str | None = None,
) -> Backup:
    """Prepara la copia sin escribirla todavia."""
    values: dict[str, str] = {}
    if include_secrets:
        values = dict(secrets) if secrets is not None else collect_secrets(config, env_path)
        values = {key: value for key, value in values.items() if key in SECRET_KEYS and value}
    return Backup(
        created=created or time.strftime("%Y-%m-%dT%H:%M:%S"),
        version=__version__,
        machine=machine if machine is not None else platform.node(),
        includes_secrets=include_secrets,
        config=full_config_mapping(config),
        secrets=values,
    )


def backup_to_dict(backup: Backup) -> dict[str, Any]:
    return {
        "formato": FORMAT,
        "formato_version": FORMAT_VERSION,
        "creado": backup.created,
        "version": backup.version,
        "equipo": backup.machine,
        "incluye_credenciales": backup.includes_secrets,
        "config": backup.config,
        "secretos": dict(backup.secrets),
    }


def backup_to_json(backup: Backup) -> str:
    return json.dumps(backup_to_dict(backup), ensure_ascii=False, indent=2)


def write_backup(backup: Backup, path: Path | str) -> Path:
    """Escribe la copia de forma atomica y devuelve la ruta final."""
    target = Path(path)
    write_atomic(target, backup_to_json(backup) + "\n")
    _restrict(target)
    log.info(
        "copia de seguridad escrita en %s (%s)",
        target,
        "con credenciales" if backup.secrets else "sin credenciales",
    )
    return target


def _restrict(path: Path) -> None:
    """Deja el fichero solo para el usuario, en los sistemas que lo permiten.

    En Windows `chmod` solo cambia el bit de solo lectura; la proteccion real es
    la ACL del perfil, que ya es privada. No es un error que falle.
    """
    with suppress(OSError):
        os.chmod(path, 0o600)


def export_backup(
    config: AppConfig,
    path: Path | str,
    *,
    include_secrets: bool = True,
    env_path: Path | None = None,
    secrets: Mapping[str, str] | None = None,
) -> Path:
    """Atajo: prepara y escribe la copia."""
    return write_backup(
        build_backup(config, include_secrets=include_secrets, env_path=env_path, secrets=secrets),
        path,
    )


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------


def _text(data: Mapping[str, Any], key: str, default: str = "") -> str:
    value = data.get(key)
    return default if value is None else str(value)


def read_backup(path: Path | str) -> Backup:
    """Lee y valida una copia. No escribe nada."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise BackupError(f"{target}: no se pudo leer: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BackupError(
            f"{target}: no es una copia válida de la alarma (JSON inválido en la línea "
            f"{exc.lineno})."
        ) from exc
    if not isinstance(raw, dict):
        raise BackupError(f"{target}: la copia no contiene un objeto JSON.")

    marker = raw.get("formato")
    if marker != FORMAT:
        raise BackupError(
            f"{target}: este fichero no es una copia completa de la alarma. "
            "Usa «Restaurar copia completa» solo con ficheros .alarmbackup."
        )
    version = raw.get("formato_version", FORMAT_VERSION)
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise BackupError(
            f"{target}: la copia se hizo con una versión más nueva del programa "
            f"(formato {version}). Actualiza la aplicación para restaurarla."
        )

    config = raw.get("config", {})
    if not isinstance(config, dict):
        raise BackupError(f"{target}: la sección «config» está dañada.")
    secrets_raw = raw.get("secretos", {})
    if not isinstance(secrets_raw, dict):
        raise BackupError(f"{target}: la sección «secretos» está dañada.")
    secrets = {
        str(key): str(value)
        for key, value in secrets_raw.items()
        if key in SECRET_KEYS and str(value)
    }

    # Validar aqui, antes de tocar ningun fichero: si el TOML resultante no
    # cargara, es mejor decirlo ahora que dejar la aplicacion sin arrancar.
    try:
        from_mapping(config)
    except ConfigError as exc:
        raise BackupError(f"{target}: la configuración de la copia no es válida: {exc}") from exc

    return Backup(
        created=_text(raw, "creado"),
        version=_text(raw, "version"),
        machine=_text(raw, "equipo"),
        includes_secrets=bool(raw.get("incluye_credenciales", bool(secrets))),
        config=config,
        secrets=secrets,
    )


# ---------------------------------------------------------------------------
# Restauracion
# ---------------------------------------------------------------------------


def restore(
    backup: Backup,
    *,
    local_path: Path | None = None,
    env_path: Path | None = None,
) -> Restored:
    """Escribe la copia en la capa local y en `.env`.

    No recarga la configuracion ni reinicia nada: eso es cosa de quien llama
    (la ventana recarga en caliente; la consola avisa de que hay que reiniciar).
    """
    target_local = LOCAL_FILE if local_path is None else Path(local_path)
    target_env = ENV_FILE if env_path is None else Path(env_path)

    text = dump_toml(backup.config, header=HEADER)
    write_atomic(target_local, text)
    # Ida y vuelta: si el serializador se comiera algo, mejor saberlo aqui.
    merged = read_toml(target_local)
    if merged != backup.config:
        log.debug("la capa local restaurada difiere del JSON; se conserva la escrita")

    written_env: Path | None = None
    if backup.secrets:
        written_env = update_env(target_env, backup.secrets)

    log.info(
        "copia restaurada en %s (%s) con %d secreto(s)",
        target_local,
        ", ".join(backup.sections),
        len(backup.secrets),
    )
    return Restored(
        local_path=target_local,
        env_path=written_env,
        secret_keys=backup.secret_keys,
        sections=backup.sections,
    )


def import_backup(
    source: Path | str,
    *,
    local_path: Path | None = None,
    env_path: Path | None = None,
) -> Restored:
    """Atajo: lee la copia y la aplica."""
    return restore(read_backup(source), local_path=local_path, env_path=env_path)


__all__ = [
    "EXTENSION",
    "FORMAT",
    "FORMAT_VERSION",
    "SECRET_KEYS",
    "Backup",
    "BackupError",
    "Restored",
    "backup_to_dict",
    "backup_to_json",
    "build_backup",
    "collect_secrets",
    "export_backup",
    "full_config_mapping",
    "import_backup",
    "read_backup",
    "restore",
    "write_backup",
]
