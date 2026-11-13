"""Volcado de la configuracion a JSON, sin credenciales.

Sirve para adjuntar como esta montada la aplicacion a un informe o a una
consulta de soporte. No es un fichero de restauracion: recorre los campos
reales de `AppConfig`, asi que una seccion nueva aparece sola, pero la lectura
de vuelta no esta soportada.

Las credenciales nunca salen: usuario y contraseña SMTP y el token de Twilio
quedan en blanco y se listan en `credenciales_omitidas`, para que quien lea el
JSON sepa que el valor existe y no esta incluido a proposito.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import __version__
from .config import AppConfig
from .settings_store import write_atomic

#: Campos que se vacian en el volcado, con la ruta que se publica.
REDACTED_FIELDS: tuple[tuple[str, str], ...] = (
    ("email", "user"),
    ("email", "password"),
    ("sms", "token"),
    ("ntfy", "token"),
    ("remote", "key"),
    ("remote", "totp_secret"),
)

FORMAT_VERSION = 1


def _plain(value: Any) -> Any:
    """Convierte el valor a algo que `json` sepa escribir."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def config_to_dict(config: AppConfig, *, redact: bool = True) -> dict[str, Any]:
    """Proyeccion completa de la configuracion en tipos JSON.

    Con `redact=True` (lo normal) las credenciales quedan en blanco. Se deja
    desactivar para pruebas y para un volcado interno de depuracion.
    """
    data = _plain(config)
    if not isinstance(data, dict):  # pragma: no cover - AppConfig es una dataclass
        raise TypeError("la configuracion no es una dataclass")

    omitted: list[str] = []
    if redact:
        for section, field_name in REDACTED_FIELDS:
            table = data.get(section)
            if isinstance(table, dict) and field_name in table:
                table[field_name] = ""
                omitted.append(f"{section}.{field_name}")

    header: dict[str, Any] = {
        "formato": FORMAT_VERSION,
        "generado": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "version": __version__,
        "origen": config.source,
        "credenciales_omitidas": omitted,
    }
    return {**header, **{key: value for key, value in data.items() if key != "source"}}


def config_to_json(config: AppConfig, *, redact: bool = True) -> str:
    return json.dumps(
        config_to_dict(config, redact=redact),
        ensure_ascii=False,
        indent=2,
    )


def export_config_json(config: AppConfig, path: Path | str) -> Path:
    """Escribe el volcado de forma atomica y devuelve la ruta final."""
    target = Path(path)
    write_atomic(target, config_to_json(config) + "\n")
    return target


__all__ = [
    "FORMAT_VERSION",
    "REDACTED_FIELDS",
    "config_to_dict",
    "config_to_json",
    "export_config_json",
]
