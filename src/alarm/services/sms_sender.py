"""Avisos por SMS con dos proveedores intercambiables.

`twilio` usa la API HTTP con `urllib` (biblioteca estandar: no anade
dependencias). `email_gateway` reutiliza el SMTP y manda el texto a la direccion
`<numero>@<dominio>` que el operador asigne.

La pasarela correo-a-SMS es gratuita pero cada vez menos fiable: la mayoria de
operadores espanoles la han retirado o la limitan. Por eso el proveedor se elige
en configuracion y no se asume ninguno.

Ambos canales comparten el limite de 160 caracteres por segmento: se recorta el
cuerpo antes de enviarlo en vez de dejar que el operador lo parta en tres
mensajes.
"""

from __future__ import annotations

import base64
import logging
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from ..config import SmsConfig
from .base import DeliveryResult, http_post, sanitize
from .email_sender import EmailChannel

log = logging.getLogger(__name__)

SMS_MAX_CHARS = 160
TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

# (url, datos del formulario, cabeceras, timeout) -> (codigo, cuerpo)
HttpTransport = Callable[[str, bytes, dict[str, str], float], tuple[int, str]]


class EmailLike(Protocol):
    """Lo que la pasarela SMS necesita del canal de correo.

    Se declara como protocolo para poder inyectar un sustituto en pruebas sin
    arrastrar `EmailChannel` entero.
    """

    @property
    def configured(self) -> bool: ...

    def send(
        self, subject: str, body: str, *, to: tuple[str, ...] | None = None
    ) -> DeliveryResult: ...


def clip(text: str, limit: int = SMS_MAX_CHARS) -> str:
    """Recorta a un segmento de SMS, sin cortar a mitad de una palabra."""
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    cut = clean[: limit - 1].rsplit(" ", 1)[0]
    return (cut or clean[: limit - 1]) + "…"


def normalize_number(number: str) -> str:
    """Deja el numero en formato E.164 aproximado.

    Acepta `+34 600 11 22 33`, `600112233` o `0034600112233`. Sin prefijo
    internacional se asume Espana, que es donde corre esto. Un `+` inicial
    siempre se respeta.
    """
    compact = "".join(ch for ch in number if ch.isdigit() or ch == "+")
    if compact.startswith("+"):
        return compact
    if compact.startswith("00"):
        return "+" + compact[2:]
    if not compact:
        return ""
    return "+34" + compact


@dataclass(slots=True)
class TwilioSmsChannel:
    """SMS por la API de Twilio."""

    config: SmsConfig
    transport: HttpTransport = http_post
    name: str = "sms"
    _enabled: bool | None = None

    def __post_init__(self) -> None:
        self._enabled = self.config.enabled

    @property
    def enabled(self) -> bool:
        return bool(self._enabled) and self.config.enabled

    @property
    def configured(self) -> bool:
        return self.config.configured

    def enabled_for(self, cause: str) -> bool:
        return self.config.enabled_for(cause)

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def reconfigure(self, config: SmsConfig) -> None:
        self.config = config
        self._enabled = config.enabled

    def send(
        self,
        subject: str,
        body: str,
        *,
        to: Sequence[str] | None = None,
        cause: str = "",
    ) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(self.name, False, "canal deshabilitado")
        if not self.config.configured:
            return DeliveryResult(
                self.name,
                False,
                f"faltan datos de Twilio: {', '.join(self.config.missing_fields)}",
            )

        numbers = [normalize_number(n) for n in (self.config.to if to is None else to)]
        numbers = [n for n in numbers if n]
        if not numbers:
            return DeliveryResult(self.name, False, "sin numeros validos")

        message = clip(body)
        url = TWILIO_URL.format(sid=self.config.sid)
        auth = base64.b64encode(f"{self.config.sid}:{self.config.token}".encode()).decode("ascii")
        headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

        delivered: list[str] = []
        last_error = ""
        for number in numbers:
            form = urllib.parse.urlencode(
                {"To": number, "From": self.config.from_number, "Body": message}
            ).encode()
            attempts = self.config.retries + 1
            for attempt in range(1, attempts + 1):
                try:
                    status, response = self.transport(url, form, headers, self.config.timeout_s)
                except Exception as exc:  # noqa: BLE001 - se reintenta cualquier fallo de red
                    last_error = sanitize(exc, self.config.token)
                    continue
                if 200 <= status < 300:
                    delivered.append(number)
                    break
                last_error = f"HTTP {status}: {response.strip()[:160]}"
                log.warning(
                    "intento %d/%d de SMS a %s fallido: %s",
                    attempt,
                    attempts,
                    number,
                    last_error,
                )

        if delivered:
            detail = f"enviado a {', '.join(delivered)}"
            if len(delivered) < len(numbers):
                detail += f" · {len(numbers) - len(delivered)} fallidos"
            return DeliveryResult(self.name, True, detail)
        return DeliveryResult(self.name, False, last_error or "sin respuesta del proveedor")


@dataclass(slots=True)
class EmailGatewaySmsChannel:
    """SMS a traves de la pasarela correo-a-SMS del operador.

    Se apoya en `EmailChannel`, que ya sabe de SMTP, reintentos y timeouts. El
    unico matiz es que cada numero es un destinatario distinto, asi que se
    envia un correo por numero: una pasarela rechaza varios destinatarios.
    """

    config: SmsConfig
    email: EmailLike
    name: str = "sms"
    _enabled: bool | None = None

    def __post_init__(self) -> None:
        self._enabled = self.config.enabled

    @property
    def enabled(self) -> bool:
        return bool(self._enabled) and self.config.enabled

    @property
    def configured(self) -> bool:
        return self.config.configured

    def enabled_for(self, cause: str) -> bool:
        return self.config.enabled_for(cause)

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def reconfigure(self, config: SmsConfig) -> None:
        self.config = config
        self._enabled = config.enabled

    def send(
        self,
        subject: str,
        body: str,
        *,
        to: Sequence[str] | None = None,
        cause: str = "",
    ) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(self.name, False, "canal deshabilitado")
        if not self.config.gateway_domain:
            return DeliveryResult(self.name, False, "falta el dominio de la pasarela")
        if not self.email.configured:
            return DeliveryResult(self.name, False, "faltan credenciales SMTP en .env")

        numbers = [normalize_number(n) for n in (self.config.to if to is None else to)]
        numbers = [n for n in numbers if n]
        if not numbers:
            return DeliveryResult(self.name, False, "sin numeros validos")

        message = clip(body)
        delivered = 0
        last_error = ""
        for number in numbers:
            recipient = f"{number.lstrip('+')}@{self.config.gateway_domain}"
            result = self.email.send(subject, message, to=(recipient,))
            if result.ok:
                delivered += 1
            else:
                last_error = result.detail

        if delivered:
            return DeliveryResult(
                self.name, True, f"enviado a {delivered} de {len(numbers)} numeros"
            )
        return DeliveryResult(self.name, False, last_error or "sin respuesta de la pasarela")


class SmsSwitch:
    """Canal unico que delega en el proveedor activo.

    El notificador ve un solo canal `sms`; por dentro se elige el proveedor. Se
    evita asi que dos canales SMS con el mismo nombre se pisen en el registro.
    """

    name = "sms"

    def __init__(
        self,
        config: SmsConfig,
        *,
        twilio: TwilioSmsChannel | None = None,
        gateway: EmailGatewaySmsChannel | None = None,
    ) -> None:
        self._config = config
        self._twilio = twilio or TwilioSmsChannel(config)
        self._gateway = gateway or EmailGatewaySmsChannel(config, _NullEmail())
        self._apply_provider()

    def _apply_provider(self) -> None:
        active = self._config.provider == "email_gateway"
        self._twilio.set_enabled(self._config.enabled and not active)
        self._gateway.set_enabled(self._config.enabled and active)

    @property
    def provider(self) -> TwilioSmsChannel | EmailGatewaySmsChannel:
        return self._gateway if self._config.provider == "email_gateway" else self._twilio

    @property
    def provider_name(self) -> str:
        return self._config.provider

    @property
    def enabled(self) -> bool:
        return self._config.enabled and self.provider.enabled

    @property
    def configured(self) -> bool:
        return self.provider.configured

    def enabled_for(self, cause: str) -> bool:
        return self._config.enabled_for(cause)

    def set_enabled(self, value: bool) -> None:
        self._config = replace(self._config, enabled=bool(value))
        self.reconfigure(self._config)

    def reconfigure(self, config: SmsConfig) -> None:
        self._config = config
        self._twilio.reconfigure(config)
        self._gateway.reconfigure(config)
        self._apply_provider()

    def send(self, subject: str, body: str, *, cause: str = "") -> DeliveryResult:
        return self.provider.send(subject, body, cause=cause)


class _NullEmail:
    """Sustituto cuando la pasarela no se usa; evita ramas nulas."""

    @property
    def configured(self) -> bool:
        return False

    def send(
        self,
        subject: str,
        body: str,
        *,
        to: tuple[str, ...] | None = None,
        cause: str = "",
    ) -> DeliveryResult:
        return DeliveryResult("email", False, "sin canal de correo")


def build_sms_channel(config: SmsConfig, email: EmailChannel) -> SmsSwitch:
    """Canal SMS listo para el notificador, con el proveedor ya seleccionado."""
    return SmsSwitch(
        config,
        twilio=TwilioSmsChannel(config),
        gateway=EmailGatewaySmsChannel(config, email),
    )
