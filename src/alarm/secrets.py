"""Escritura de secretos en `.env`.

El usuario no deberia tener que editar a mano un fichero de entorno para poner
la contrasena de aplicacion de Gmail o el token de Twilio. Los dialogos lo
hacen por el, con dos reglas:

- Nunca se registra un valor, solo la clave que se ha actualizado.
- El resto del fichero se conserva tal cual: comentarios, orden y claves
  desconocidas. Se sustituye la linea de la clave y se anaden las que falten.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from .config import ENV_FILE, parse_env
from .settings_store import write_atomic

log = logging.getLogger(__name__)

# Claves que la aplicacion escribe. Cualquier otra se deja intacta.
KNOWN_KEYS: tuple[str, ...] = (
    "ALARM_SMTP_USER",
    "ALARM_SMTP_PASSWORD",
    "ALARM_TWILIO_SID",
    "ALARM_TWILIO_TOKEN",
    "ALARM_NTFY_TOKEN",
    "ALARM_REMOTE_KEY",
    "ALARM_REMOTE_TOTP",
)


def _format(line_key: str, value: str) -> str:
    needs_quotes = not value or any(ch in value for ch in " #\t'\"")
    if needs_quotes:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{line_key}="{escaped}"'
    return f"{line_key}={value}"


def update_env(path: Path | None, values: Mapping[str, str]) -> Path:
    """Aplica `values` a `.env` conservando el resto del contenido."""
    target = ENV_FILE if path is None else Path(path)
    existing: list[str] = []
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8").splitlines()
        except OSError as exc:  # pragma: no cover - permisos
            log.warning("no se pudo leer %s: %s", target, exc)
            existing = []

    pending = dict(values)
    output: list[str] = []
    for raw in existing:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(raw)
            continue
        candidate = stripped[len("export ") :] if stripped.startswith("export ") else stripped
        key = candidate.partition("=")[0].strip()
        if key in pending:
            output.append(_format(key, pending.pop(key)))
        else:
            output.append(raw)

    if pending:
        if output and output[-1].strip():
            output.append("")
        output.extend(_format(key, value) for key, value in pending.items())

    write_atomic(target, "\n".join(output).rstrip("\n") + "\n")
    # Solo se registran las claves, jamas los valores.
    log.info("secretos actualizados en %s: %s", target, ", ".join(sorted(values)))
    return target


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """Lee `.env` sin fusionarlo con el entorno del proceso."""
    target = ENV_FILE if path is None else Path(path)
    if not target.is_file():
        return {}
    try:
        return parse_env(target.read_text(encoding="utf-8"))
    except OSError:  # pragma: no cover - permisos
        return {}
