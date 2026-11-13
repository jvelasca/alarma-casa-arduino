"""Codigos de un solo uso (TOTP) para autenticar las ordenes del movil.

En el servicio publico de ntfy un tema no es privado: quien conozca su nombre
lee los mensajes. Una clave escrita en el mensaje sirve frente a quien adivine
el tema, pero no frente a quien ya lo esta leyendo. Un codigo TOTP resuelve eso
porque **nunca viaja en la orden**: el PC lo calcula a partir del secreto y el
movil lo genera con la hora.

Solo biblioteca estandar: `base64`, `hashlib`, `hmac` y `struct`. Es el mismo
algoritmo que usan Google Authenticator, Authy y compania (RFC 6238), asi que
el secreto que genera esta aplicacion se puede dar de alta en cualquiera de
ellas con la URI `otpauth://`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

#: Lo que dura un codigo. 30 s es lo habitual y lo que esperan las apps.
STEP_S = 30
#: Cifras del codigo. Seis, otra vez lo habitual.
DIGITS = 6
#: Pasos de tolerancia hacia atras y adelante. Relojes desincronizados unos
#: segundos no deben dejar al usuario fuera de su propia casa.
WINDOW = 1


def generate_secret() -> str:
    """Secreto nuevo, en base32 sin relleno (lo que aceptan las apps)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def normalise_secret(secret: str) -> str:
    """Deja el secreto comparable: sin espacios, guiones ni relleno, en mayusculas.

    La gente copia la clave de la app de autenticacion con espacios cada cuatro
    caracteres. Rechazarla por eso seria absurdo.
    """
    text = secret.strip().upper()
    for junk in (" ", "-", "_", "="):
        text = text.replace(junk, "")
    return text


def _decode_secret(secret: str) -> bytes:
    normalised = normalise_secret(secret)
    if not normalised:
        raise ValueError("secreto TOTP vacío")
    # `b32decode` exige el relleno que se recorta al generar la clave.
    padded = normalised + "=" * (-len(normalised) % 8)
    try:
        return base64.b32decode(padded, casefold=True)
    except Exception as exc:  # noqa: BLE001 - se informa como valor no valido
        raise ValueError("secreto TOTP no válido (no es base32)") from exc


def code_at(
    secret: str, at: float | None = None, *, digits: int = DIGITS, step: int = STEP_S
) -> str:
    """Codigo valido en el instante `at`, rellenado con ceros por delante."""
    moment = time.time() if at is None else float(at)
    counter = int(moment // max(1, step))
    digest = hmac.new(_decode_secret(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    chunk = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(chunk % (10**digits)).zfill(digits)


def verify_code(
    secret: str,
    code: str,
    at: float | None = None,
    *,
    window: int = WINDOW,
    digits: int = DIGITS,
    used: set[int] | None = None,
) -> bool:
    """Si el codigo corresponde a alguno de los pasos cercanos a `at`.

    `used` es el conjunto de pasos ya gastados: un codigo no se puede reutilizar
    aunque siga dentro de su ventana de 30 s. Sin esto, quien lea el mensaje
    podria repetirlo tal cual y el codigo dejaria de ser de un solo uso.
    """
    moment = time.time() if at is None else float(at)
    normalised = code.strip().replace(" ", "")
    if not normalised.isdigit() or len(normalised) != digits:
        return False
    current = int(moment // STEP_S)
    for offset in range(-window, window + 1):
        step_index = current + offset
        if used is not None and step_index in used:
            continue
        try:
            expected = code_at(secret, step_index * STEP_S, digits=digits)
        except ValueError:
            return False
        if hmac.compare_digest(expected, normalised):
            if used is not None:
                used.add(step_index)
            return True
    return False


def provisioning_uri(secret: str, *, label: str = "alarma-casa", issuer: str = "Alarma") -> str:
    """URI `otpauth://` para dar de alta el secreto en una app de autenticacion.

    Sin QR: dibujarlo pediria una dependencia grafica que el proyecto no tiene,
    y la URI se puede copiar y pegar en las apps que la aceptan.
    """
    label_text = urllib.parse.quote(f"{issuer}:{label}", safe="")
    params = urllib.parse.urlencode(
        {
            "secret": normalise_secret(secret),
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP_S,
        }
    )
    return f"otpauth://totp/{label_text}?{params}"


__all__ = [
    "DIGITS",
    "STEP_S",
    "WINDOW",
    "code_at",
    "generate_secret",
    "normalise_secret",
    "provisioning_uri",
    "verify_code",
]
