"""Avisos por correo electronico con SMTP.

Solo biblioteca estandar: `smtplib` y `email.message`. El canal es un objeto
mas de la cadena de avisos: si manana se anade Telegram, el notificador no
cambia.

Dos detalles que se cuidan a proposito:

- Los errores se sanean antes de registrarse. Una excepcion de `smtplib` puede
  arrastrar el comando enviado; la contrasena nunca debe acabar en el log.
- Cada intento tiene timeout explicito. Sin el, un servidor que acepta la
  conexion y no contesta deja el hilo de avisos colgado para siempre.

Y dos que evitan diagnosticos enganosos:

- El cifrado se deduce del puerto cuando el TOML trae una pareja imposible:
  465 sin SSL (o 587 con SSL) hace que el servidor corte al saludar.
- La autenticacion usa un solo mecanismo. `smtplib.login()` los encadena y, si
  el primero devuelve 535, Gmail cierra la conexion en el segundo: el corte
  tapaba el verdadero «usuario o contrasena no aceptados».
"""

from __future__ import annotations

import base64
import logging
import re
import smtplib
import ssl
import time
from collections.abc import Callable
from contextlib import suppress
from email.message import EmailMessage

from ..config import EmailConfig
from .base import DeliveryResult, sanitize

log = logging.getLogger(__name__)

Sender = Callable[[EmailMessage], None]

#: Puertos que esperan TLS desde el primer byte (SMTPS implicito).
SSL_PORTS = frozenset({465, 994})
#: Puertos que empiezan en claro y suben a TLS con STARTTLS.
STARTTLS_PORTS = frozenset({25, 587, 2525, 1025})

#: Puerto convencional de cada modo. La ventana los usa al cambiar de modo.
SSL_PORT = 465
STARTTLS_PORT = 587

_STAGE_HINTS = {
    "conexión": "comprueba host y puerto, y que la red permita correo saliente",
    "TLS": "el servidor rechazó el cifrado: 587 usa STARTTLS y 465 usa SSL",
    "autenticación": (
        "usuario o contraseña rechazados: en Gmail usa la contraseña de aplicación "
        "de 16 caracteres (sin espacios) de esa misma cuenta"
    ),
    "envío": "revisa el remitente y los destinatarios",
}


class SmtpError(RuntimeError):
    """Fallo de transporte con la etapa donde ocurrio y una pista accionable."""


def effective_security(port: int, security: str) -> str:
    """Cifrado que ese puerto admite de verdad.

    La combinacion incoherente (465 con STARTTLS o 587 con SSL) es la causa
    tipica de `SMTPServerDisconnected: Connection unexpectedly closed`: el
    cliente saluda en claro donde el servidor espera TLS (o al reves) y el
    servidor corta. Corregirla aqui evita que un ajuste viejo tumbe el aviso.
    """
    if security == "starttls" and port in SSL_PORTS:
        return "ssl"
    if security == "ssl" and port in STARTTLS_PORTS:
        return "starttls"
    return security


def _stage_error(stage: str, target: str, security: str, exc: BaseException) -> SmtpError:
    """Traduce un fallo de `smtplib` a una frase con etapa y pista.

    La pista va delante del texto del servidor: `send()` recorta el detalle a
    300 caracteres y el mensaje util no debe ser lo que se pierda.
    """
    detail = sanitize(exc, "")[:120]
    hint = _STAGE_HINTS.get(stage, "")
    if isinstance(exc, smtplib.SMTPServerDisconnected) and stage in {"conexión", "TLS"}:
        hint = (
            f"el servidor cerró la conexión al saludar con {security}: "
            "587 usa STARTTLS y 465 usa SSL"
        )
    elif isinstance(exc, ssl.SSLError) and stage in {"conexión", "TLS"}:
        hint = "el servidor no habla TLS en ese puerto: 587 usa STARTTLS y 465 usa SSL"
    prefix = f"{stage} con {target}:"
    return SmtpError(f"{prefix} {hint} · {detail}" if hint else f"{prefix} {detail}")


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


#: Contraseña de aplicación tal y como la muestra Google: 4 grupos de 4.
_APP_PASSWORD = re.compile(r"(?:[A-Za-z0-9]{4}[\s-]+){3}[A-Za-z0-9]{4}")


def _auth_password(password: str) -> str:
    """Contraseña lista para AUTH, sin separadores de cortesia.

    Google muestra la contraseña de aplicación como «abcd efgh ijkl mnop».
    Pegada tal cual, el servidor la rechaza. Solo se toca una cadena que sea
    exactamente ese patron, para no alterar contraseñas normales.
    """
    stripped = password.strip()
    if not _APP_PASSWORD.fullmatch(stripped):
        return stripped
    joined = re.sub(r"[\s-]", "", stripped)
    log.info("contraseña de aplicación con separadores: se usa sin espacios")
    return joined


def _authenticate(server: smtplib.SMTP, user: str, password: str) -> None:
    """Un solo mecanismo de autenticacion, nunca una cadena de intentos.

    `smtplib.login()` prueba todos los mecanismos anunciados. Si el primero
    devuelve 535, algunos servidores (Gmail) cierran la conexion al ver el
    segundo, y ese corte tapa el motivo real. Aqui se elige uno y se usa una
    vez: el 535 llega intacto al usuario.
    """
    secret = _auth_password(password)
    methods = {name.upper() for name in (server.esmtp_features.get("auth") or "").split()}
    auth = getattr(server, "auth", None)

    if callable(auth) and "PLAIN" in methods:
        auth("PLAIN", lambda: f"\0{user}\0{secret}")
        return

    if "LOGIN" in methods:
        # `SMTP.auth` solo cubre un reto; LOGIN pide usuario y despues clave.
        code, resp = server.docmd("AUTH", "LOGIN " + _b64(user))
        if code == 334:
            code, resp = server.docmd(_b64(secret))
        if code not in (235, 503):
            raise smtplib.SMTPAuthenticationError(code, resp)
        return

    server.login(user, secret)


def _permanent(exc: BaseException) -> bool:
    """Errores que no mejoran reintentando: credenciales, TLS o direcciones."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (
                smtplib.SMTPAuthenticationError,
                smtplib.SMTPNotSupportedError,
                smtplib.SMTPRecipientsRefused,
                smtplib.SMTPSenderRefused,
                ssl.SSLError,
            ),
        ):
            return True
        if isinstance(current, smtplib.SMTPResponseException) and current.smtp_code >= 500:
            return True
        current = current.__cause__ or current.__context__
    return False


class EmailChannel:
    """Envio por SMTP con reintentos y retroceso exponencial."""

    name = "email"

    def __init__(
        self,
        config: EmailConfig,
        *,
        sender: Sender | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self._sender = sender
        self._sleep = sleep
        self._enabled = config.enabled

    @property
    def enabled(self) -> bool:
        return self._enabled and self.config.enabled

    @property
    def configured(self) -> bool:
        return self.config.configured

    def enabled_for(self, cause: str) -> bool:
        """Si este evento concreto debe generar correo."""
        return self.config.enabled_for(cause)

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def reconfigure(self, config: EmailConfig) -> None:
        self.config = config
        self._enabled = config.enabled

    def send(
        self,
        subject: str,
        body: str,
        *,
        to: tuple[str, ...] | None = None,
        cause: str = "",
    ) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(self.name, False, "canal deshabilitado")
        if not self.config.configured:
            return DeliveryResult(
                self.name,
                False,
                "faltan datos: host, remitente, destinatarios o credenciales (.env)",
            )

        recipients = self.config.to if to is None else to
        if not recipients:
            return DeliveryResult(self.name, False, "sin destinatarios")

        message = EmailMessage()
        message["From"] = self.config.sender
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)

        deliver = self._sender or self._send_smtp
        attempts = self.config.retries + 1
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                deliver(message)
            except Exception as exc:  # noqa: BLE001 - se reintenta cualquier fallo de red
                last_error = sanitize(exc, self.config.password)
                log.warning("intento %d/%d de correo fallido: %s", attempt, attempts, last_error)
                # Reintentar una contraseña rechazada solo acumula bloqueos en
                # el proveedor; un corte de red, en cambio, sí merece otro giro.
                if _permanent(exc) or attempt >= attempts:
                    break
                self._sleep(min(2.0**attempt, 30.0))
            else:
                log.info("correo enviado a %s", message["To"])
                return DeliveryResult(self.name, True, f"enviado a {message['To']}")

        return DeliveryResult(self.name, False, last_error or "fallo desconocido")

    # -- Transporte por defecto -------------------------------------------

    def _send_smtp(self, message: EmailMessage) -> None:
        cfg = self.config
        security = effective_security(cfg.port, cfg.security)
        if security != cfg.security:
            log.info("el puerto %d no admite %s: se usa %s", cfg.port, cfg.security, security)
        target = f"{cfg.host}:{cfg.port}"
        context = ssl.create_default_context()

        try:
            if security == "ssl":
                server: smtplib.SMTP = smtplib.SMTP_SSL(
                    cfg.host, cfg.port, timeout=cfg.timeout_s, context=context
                )
            else:
                server = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_s)
        except Exception as exc:  # noqa: BLE001 - se convierte en un mensaje legible
            raise _stage_error("conexión", target, security, exc) from exc

        try:
            try:
                server.ehlo_or_helo_if_needed()
            except Exception as exc:  # noqa: BLE001
                raise _stage_error("conexión", target, security, exc) from exc

            if security == "starttls":
                try:
                    server.starttls(context=context)
                    server.ehlo_or_helo_if_needed()
                except Exception as exc:  # noqa: BLE001
                    raise _stage_error("TLS", target, security, exc) from exc

            if cfg.user:
                try:
                    _authenticate(server, cfg.user, cfg.password)
                except Exception as exc:  # noqa: BLE001
                    raise _stage_error("autenticación", target, security, exc) from exc

            try:
                server.send_message(message)
            except Exception as exc:  # noqa: BLE001
                raise _stage_error("envío", target, security, exc) from exc
        finally:
            # Cerrar nunca debe tapar el error real del envio.
            with suppress(Exception):
                server.quit()
