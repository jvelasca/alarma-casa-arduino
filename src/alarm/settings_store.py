"""Guardado de la configuracion escrita desde la interfaz.

Los dialogos no reescriben ``config/alarm.toml``: un TOML generado por codigo
perderia todos los comentarios y el orden que el usuario haya puesto. En su
lugar se escribe ``config/alarm.local.toml``, que se superpone a la base al
cargar.

El serializador cubre solo lo que la aplicacion escribe (booleanos, enteros,
decimales, textos, listas de textos y tablas de hasta dos niveles). No pretende
ser un TOML general: para algo mas complejo, el usuario edita el fichero base.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    LOCAL_FILE,
    VOICE_KEYS,
    AppConfig,
    ConfigError,
    deep_merge,
    read_toml,
)

log = logging.getLogger(__name__)

HEADER = """\
# Configuracion escrita desde la aplicacion.
#
# Se superpone a `config/alarm.toml`: aqui solo aparecen los valores que se han
# cambiado desde los dialogos. Las credenciales no estan en este fichero, sino
# en `.env`. Para volver a los valores del ejemplo, borra este fichero.
"""


def format_value(value: Any) -> str:
    """Literal TOML de un escalar o de una lista de escalares."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    if isinstance(value, Sequence):
        inner = ", ".join(format_value(item) for item in value)
        return f"[{inner}]"
    raise ConfigError(f"no se puede escribir en TOML: {type(value).__name__}")


def _emit_table(lines: list[str], path: tuple[str, ...], table: Mapping[str, Any]) -> None:
    """Escribe una tabla con sus escalares y sus subtablas anidadas."""
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    nested = {k: v for k, v in table.items() if isinstance(v, dict)}

    if scalars:
        if path:
            lines.append(f"[{'.'.join(path)}]")
        for key, value in scalars.items():
            lines.append(f"{key} = {format_value(value)}")
        lines.append("")

    for key, child in nested.items():
        _emit_table(lines, (*path, key), child)


def dump_toml(data: Mapping[str, Any], *, header: str | None = None) -> str:
    """Serializa un diccionario anidado a texto TOML.

    `header` permite cambiar el comentario de cabecera: la restauracion de una
    copia de seguridad escribe el mismo formato, pero conviene que el fichero
    diga de donde salio.
    """
    lines: list[str] = [HEADER if header is None else header]
    _emit_table(lines, (), data)
    # Colapsar los saltos de linea repetidos del final.
    text = "\n".join(lines).rstrip("\n") + "\n"
    return text


def config_to_mapping(config: AppConfig) -> dict[str, Any]:
    """Proyeccion de la configuracion que la interfaz puede editar."""
    events: dict[str, Any] = {}
    for key in VOICE_KEYS:
        event = config.tts.event(key)
        events[key] = {
            "enabled": event.enabled,
            "text": event.text,
            "label": event.label,
            "repeat": event.repeat,
            "gap_ms": event.gap_ms,
            "delay_ms": event.delay_ms,
            "log": event.log,
        }

    return {
        "alarm": {
            "armed_on_start": config.alarm.armed_on_start,
            "cooldown_s": config.alarm.cooldown_s,
            "max_per_hour": config.alarm.max_per_hour,
            "exit_delay_s": config.alarm.exit_delay_s,
            "entry_delay_s": config.alarm.entry_delay_s,
            "auto_rearm": config.alarm.auto_rearm,
            "rearm_delay_s": config.alarm.rearm_delay_s,
            "max_rearms": config.alarm.max_rearms,
        },
        "tts": {
            "enabled": config.tts.enabled,
            "voice": config.tts.voice,
            "rate": config.tts.rate,
            "volume": config.tts.volume,
            "repeat": config.tts.repeat,
            "events": events,
        },
        "notify": {
            "email": {
                "enabled": config.email.enabled,
                "on_armed": config.email.on_armed,
                "on_disarmed": config.email.on_disarmed,
                "on_triggered": config.email.on_triggered,
                "host": config.email.host,
                "port": config.email.port,
                "security": config.email.security,
                "from": config.email.sender,
                "to": list(config.email.to),
                "subject": config.email.subject,
                "timeout_s": config.email.timeout_s,
                "retries": config.email.retries,
                "on_device_lost": config.email.on_device_lost,
            },
            "sms": {
                "enabled": config.sms.enabled,
                "provider": config.sms.provider,
                "on_armed": config.sms.on_armed,
                "on_disarmed": config.sms.on_disarmed,
                "on_triggered": config.sms.on_triggered,
                "to": list(config.sms.to),
                "from_number": config.sms.from_number,
                "gateway_domain": config.sms.gateway_domain,
                "template": config.sms.template,
                "timeout_s": config.sms.timeout_s,
                "retries": config.sms.retries,
                "on_device_lost": config.sms.on_device_lost,
            },
            "ntfy": {
                "enabled": config.ntfy.enabled,
                "server": config.ntfy.server,
                "topic": config.ntfy.topic,
                "on_armed": config.ntfy.on_armed,
                "on_disarmed": config.ntfy.on_disarmed,
                "on_triggered": config.ntfy.on_triggered,
                "on_device_lost": config.ntfy.on_device_lost,
                "title": config.ntfy.title,
                "priority_triggered": config.ntfy.priority_triggered,
                "priority_device_lost": config.ntfy.priority_device_lost,
                "priority_armed": config.ntfy.priority_armed,
                "priority_disarmed": config.ntfy.priority_disarmed,
                "timeout_s": config.ntfy.timeout_s,
                "retries": config.ntfy.retries,
            },
        },
        "remote": {
            "enabled": config.remote.enabled,
            "topic": config.remote.topic,
            "allow_arm": config.remote.allow_arm,
            "allow_disarm": config.remote.allow_disarm,
            "allow_status": config.remote.allow_status,
            "allow_silence": config.remote.allow_silence,
            "allow_panic": config.remote.allow_panic,
            "reply": config.remote.reply,
            "lockout_failures": config.remote.lockout_failures,
            "lockout_minutes": config.remote.lockout_minutes,
        },
        "ui": {
            "labels": dict(config.ui.labels),
            "show_heartbeats": config.ui.show_heartbeats,
            "beeps": config.ui.beeps,
        },
        "buzzer": {
            "enabled": config.buzzer.enabled,
            "pin": config.buzzer.pin,
            "siren_ms": config.buzzer.siren_ms,
            "test_hz": config.buzzer.test_hz,
            "test_ms": config.buzzer.test_ms,
        },
    }


def write_atomic(path: Path, text: str) -> None:
    """Escribe a un temporal en el mismo directorio y renombra.

    Un corte a mitad de escritura no debe dejar la configuracion truncada: el
    renombrado es atomico en el mismo volumen.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp", text=True)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def save_local(config: AppConfig, path: Path | None = None) -> Path:
    """Escribe la configuracion editable en la capa local.

    Se valida antes de escribir: si algo no cuadra, la excepcion sube al dialogo
    y no se toca el fichero. Despues se relee lo escrito y se mezcla para
    comprobar que el resultado no se ha degradado.
    """
    target = LOCAL_FILE if path is None else Path(path)
    data = config_to_mapping(config)
    # Lo que ya hubiera en la capa local se conserva: los dialogos solo son
    # duenos de sus claves. Sin esto, guardar cualquier ajuste borraria las
    # secciones que no tienen dialogo (`serial`, `log`) y que pueden venir de
    # una copia de seguridad restaurada.
    data = deep_merge(read_toml(target), data)
    text = dump_toml(data)
    write_atomic(target, text)

    # Verificacion de ida y vuelta: si el serializador se equivoca, se prefiere
    # fallar aqui y no dentro de tres dias al arrancar.
    assert isinstance(deep_merge(data, read_toml(target)), dict)  # noqa: S101
    log.info("configuracion local guardada en %s", target)
    return target


@dataclass(frozen=True, slots=True)
class SettingsPatch:
    """Cambios parciales sobre la configuracion vigente.

    Cada dialogo aplica solo su parte para no pisar lo que haya cambiado otro
    mientras estaba abierto.
    """

    config: AppConfig

    def with_voice_event(self, key: str, event: Any) -> AppConfig:
        from dataclasses import replace

        events = dict(self.config.tts.events)
        events[key] = event
        return replace(self.config, tts=replace(self.config.tts, events=events))
