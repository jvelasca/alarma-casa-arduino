"""Pruebas del notificador y del canal de correo."""

from __future__ import annotations

import smtplib
import threading
import time
from dataclasses import replace
from email.message import EmailMessage

import pytest

from alarm.config import EmailConfig, default_config
from alarm.services.base import DeliveryResult
from alarm.services.email_sender import EmailChannel, effective_security
from alarm.services.notifier import Notifier


class FakeChannel:
    def __init__(self, name: str = "fake", ok: bool = True, enabled: bool = True) -> None:
        self.name = name
        self.enabled = enabled
        self.configured = True
        self.ok = ok
        self.calls = 0
        self.release: threading.Event | None = None
        self.causes: list[str] = []
        self.sent_causes: list[str] = []

    def enabled_for(self, cause: str) -> bool:
        self.causes.append(cause)
        return True

    def send(self, subject: str, body: str, *, cause: str = "") -> DeliveryResult:
        self.calls += 1
        self.sent_causes.append(cause)
        if self.release is not None:
            self.release.wait(2.0)
        return DeliveryResult(self.name, self.ok, "prueba")


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_entrega_y_reporta_el_resultado() -> None:
    channel = FakeChannel()
    results: list[tuple[DeliveryResult, str]] = []
    notifier = Notifier([channel], on_result=lambda r, c: results.append((r, c)))
    try:
        assert notifier.submit("triggered", "Asunto", "Cuerpo") is True
        assert _wait(lambda: len(results) == 1)
        assert results[0][0].ok is True
        assert results[0][1] == "triggered"
        assert channel.calls == 1
    finally:
        notifier.close()


def test_sin_canales_no_encola() -> None:
    notifier = Notifier([FakeChannel(enabled=False)])
    try:
        assert notifier.active is False
        assert notifier.submit("triggered", "x", "y") is False
    finally:
        notifier.close()


def test_la_causa_llega_al_canal() -> None:
    """ntfy elige prioridad segun el evento: necesita saber cual es."""
    channel = FakeChannel()
    notifier = Notifier([channel])
    try:
        assert notifier.submit("device_lost", "x", "y") is True
        assert _wait(lambda: channel.sent_causes == ["device_lost"])
    finally:
        notifier.close()


def test_la_compuerta_bloquea() -> None:
    channel = FakeChannel()
    notifier = Notifier([channel], gate=lambda cause: cause != "triggered")
    try:
        assert notifier.submit("triggered", "x", "y") is False
        assert notifier.blocked == 1
        assert notifier.submit("armed", "x", "y") is True
        assert _wait(lambda: channel.calls == 1)
    finally:
        notifier.close()


def test_cola_llena_descarta_sin_bloquear() -> None:
    channel = FakeChannel()
    channel.release = threading.Event()
    notifier = Notifier([channel], max_queue=1)
    try:
        assert notifier.submit("uno", "x", "y") is True
        assert _wait(lambda: channel.calls == 1)  # el trabajador esta ocupado
        assert notifier.submit("dos", "x", "y") is True  # ocupa la cola
        assert notifier.submit("tres", "x", "y") is False  # cola llena
        assert notifier.dropped == 1
    finally:
        assert channel.release is not None
        channel.release.set()
        notifier.close()


def test_un_canal_que_falla_no_tumba_el_hilo() -> None:
    class Explosive(FakeChannel):
        def send(self, subject: str, body: str, *, cause: str = "") -> DeliveryResult:
            self.calls += 1
            raise RuntimeError("sin red")

    channel = Explosive()
    notifier = Notifier([channel])
    try:
        assert notifier.submit("triggered", "x", "y") is True
        assert _wait(lambda: notifier.failed == 1)
        assert notifier.failed == 1
    finally:
        notifier.close()


def test_channels_for_exige_activo_configurado_y_evento() -> None:
    activo = FakeChannel(name="correo")
    desactivado = FakeChannel(name="sms", enabled=False)
    sin_configurar = FakeChannel(name="push")
    sin_configurar.configured = False

    notifier = Notifier([activo, desactivado, sin_configurar])
    try:
        assert notifier.channels_for("triggered") == ["correo"]
        # `available` solo mira si el canal esta activo; estar configurado es
        # otro requisito y se comprueba al decidir a quien se entrega.
        assert notifier.available == ["correo", "push"]
    finally:
        notifier.close()


def test_un_canal_solo_recibe_los_eventos_que_le_tocan() -> None:
    class SoloDisparo(FakeChannel):
        def enabled_for(self, cause: str) -> bool:
            self.causes.append(cause)
            return cause == "triggered"

    channel = SoloDisparo()
    notifier = Notifier([channel])
    try:
        assert notifier.submit("armed", "Asunto", "Cuerpo") is False
        assert channel.calls == 0
        assert notifier.submit("triggered", "Asunto", "Cuerpo") is True
        assert _wait(lambda: channel.calls == 1)
    finally:
        notifier.close()


def test_ignore_gate_salta_el_enfriamiento() -> None:
    channel = FakeChannel()
    notifier = Notifier([channel], gate=lambda _cause: False)
    try:
        assert notifier.submit("triggered", "x", "y") is False
        assert notifier.blocked == 1
        assert notifier.submit("triggered", "x", "y", ignore_gate=True) is True
        assert _wait(lambda: channel.calls == 1)
    finally:
        notifier.close()


def test_reconfigure_actualiza_los_canales_sin_reiniciar() -> None:
    primero = FakeChannel(name="correo")
    notifier = Notifier([primero])
    try:
        assert notifier.available == ["correo"]
        notifier.reconfigure([FakeChannel(name="sms")])
        assert notifier.available == ["sms"]
    finally:
        notifier.close()


# ---------------------------------------------------------------------------
# EmailChannel
# ---------------------------------------------------------------------------


def _email_config() -> EmailConfig:
    config = default_config().email
    return replace(
        config,
        enabled=True,
        host="smtp.example.com",
        sender="alarma@example.com",
        to=("yo@example.com",),
        user="alarma@example.com",
        password="super-secreta",
        retries=2,
    )


def test_correo_reintenta_y_acaba_bien() -> None:
    attempts = {"n": 0}

    def sender(_message: EmailMessage) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("conexion rechazada")

    channel = EmailChannel(_email_config(), sender=sender, sleep=lambda _s: None)
    result = channel.send("Asunto", "Cuerpo")
    assert result.ok is True
    assert attempts["n"] == 3


def test_correo_agota_reintentos_y_sanea_la_contrasena() -> None:
    def sender(_message: EmailMessage) -> None:
        raise OSError("login fallido con super-secreta")

    channel = EmailChannel(_email_config(), sender=sender, sleep=lambda _s: None)
    result = channel.send("Asunto", "Cuerpo")
    assert result.ok is False
    assert "super-secreta" not in result.detail
    assert "***" in result.detail


def test_correo_sin_configurar_no_intenta_enviar() -> None:
    def sender(_message: EmailMessage) -> None:  # pragma: no cover - no debe llamarse
        raise AssertionError("no deberia enviarse")

    config = replace(_email_config(), password="")
    channel = EmailChannel(config, sender=sender, sleep=lambda _s: None)
    result = channel.send("Asunto", "Cuerpo")
    assert result.ok is False
    assert "faltan datos" in result.detail


def test_correo_deshabilitado() -> None:
    channel = EmailChannel(replace(_email_config(), enabled=False))
    result = channel.send("Asunto", "Cuerpo")
    assert result.ok is False
    assert result.detail == "canal deshabilitado"


# ---------------------------------------------------------------------------
# Transporte SMTP: cifrado coherente con el puerto y errores por etapa
# ---------------------------------------------------------------------------


def _fake_smtp(
    monkeypatch: pytest.MonkeyPatch, *, ssl: bool = False, fail: Exception | None = None
) -> list[str]:
    """Sustituye `smtplib.SMTP`/`SMTP_SSL` por un servidor que apunta lo que pasa."""
    calls: list[str] = []

    if fail is not None:
        # SMTP_SSL hereda de SMTP, asi que un fallo al conectar se prueba en ambas.
        def boom(*_args: object, **_kwargs: object) -> None:
            raise fail

        monkeypatch.setattr(smtplib, "SMTP", boom)
        monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
        return calls

    class FakeServer:
        esmtp_features: dict[str, str] = {"auth": "LOGIN PLAIN"}

        def __init__(
            self, host: str, port: int, timeout: float | None = None, context=None
        ) -> None:
            calls.append(f"{'ssl' if ssl else 'plain'} {host}:{port}")

        def ehlo_or_helo_if_needed(self) -> None:
            calls.append("ehlo")

        def starttls(self, context=None) -> None:
            calls.append("starttls")

        def auth(self, mechanism: str, authobject, *, initial_response_ok: bool = True) -> None:
            calls.append(f"auth {mechanism}")
            parts = authobject().split(chr(0))
            calls.append(f"login {parts[1]}:{parts[2]}")

        def docmd(self, cmd: str, args: str = "") -> tuple[int, bytes]:  # pragma: no cover
            raise AssertionError("el servidor de prueba anuncia PLAIN")

        def send_message(self, message: EmailMessage) -> None:
            calls.append("send")

        def quit(self) -> None:
            calls.append("quit")

    monkeypatch.setattr(smtplib, "SMTP_SSL" if ssl else "SMTP", FakeServer)
    return calls


def _message() -> EmailMessage:
    message = EmailMessage()
    message["From"] = "alarma@example.com"
    message["To"] = "yo@example.com"
    message.set_content("hola")
    return message


def test_effective_security_corrige_la_pareja_imposible() -> None:
    assert effective_security(465, "starttls") == "ssl"
    assert effective_security(587, "ssl") == "starttls"
    assert effective_security(1025, "none") == "none"
    assert effective_security(2525, "starttls") == "starttls"


def test_correo_con_starttls_saluda_y_cifra_antes_de_entrar(monkeypatch) -> None:
    calls = _fake_smtp(monkeypatch)
    channel = EmailChannel(replace(_email_config(), port=587, security="starttls", retries=0))
    channel._send_smtp(_message())
    assert calls == [
        "plain smtp.example.com:587",
        "ehlo",
        "starttls",
        "ehlo",
        "auth PLAIN",
        "login alarma@example.com:super-secreta",
        "send",
        "quit",
    ]


def test_correo_usa_ssl_si_el_puerto_es_465(monkeypatch) -> None:
    calls = _fake_smtp(monkeypatch, ssl=True)
    # La pareja guardada dice STARTTLS, pero el 465 solo habla TLS desde el inicio.
    channel = EmailChannel(replace(_email_config(), port=465, security="starttls", retries=0))
    channel._send_smtp(_message())
    assert calls[0] == "ssl smtp.example.com:465"
    assert "starttls" not in calls


def test_correo_cuenta_la_etapa_y_sugiere_puerto(monkeypatch) -> None:
    _fake_smtp(monkeypatch, fail=smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
    channel = EmailChannel(replace(_email_config(), port=465, security="starttls", retries=0))
    result = channel.send("Asunto", "Cuerpo")
    assert result.ok is False
    assert "conexión con smtp.example.com:465" in result.detail
    assert "465 usa SSL" in result.detail


def test_correo_no_reintenta_una_contrasena_rechazada(monkeypatch) -> None:
    rejects = {"n": 0}

    class Rechaza:
        esmtp_features: dict[str, str] = {"auth": "LOGIN PLAIN"}

        def __init__(
            self, host: str, port: int, timeout: float | None = None, context=None
        ) -> None:
            pass

        def ehlo_or_helo_if_needed(self) -> None:
            pass

        def starttls(self, context=None) -> None:
            pass

        def auth(self, mechanism: str, authobject, *, initial_response_ok: bool = True) -> None:
            rejects["n"] += 1
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

        def send_message(self, message: EmailMessage) -> None:  # pragma: no cover
            raise AssertionError("no debe llegar a enviar")

        def quit(self) -> None:
            pass

    monkeypatch.setattr(smtplib, "SMTP", Rechaza)
    channel = EmailChannel(replace(_email_config(), port=587, security="starttls", retries=3))
    result = channel.send("Asunto", "Cuerpo")
    assert result.ok is False
    assert "autenticación" in result.detail
    assert "535" in result.detail
    assert rejects["n"] == 1


def test_correo_quita_los_separadores_de_la_contrasena_de_aplicacion(monkeypatch) -> None:
    calls = _fake_smtp(monkeypatch)
    channel = EmailChannel(
        replace(
            _email_config(),
            password="abcd efgh ijkl mnop",
            port=587,
            security="starttls",
            retries=0,
        )
    )
    channel._send_smtp(_message())
    assert "login alarma@example.com:abcdefghijklmnop" in calls


def test_correo_respeta_una_contrasena_con_espacios_que_no_es_de_aplicacion(monkeypatch) -> None:
    calls = _fake_smtp(monkeypatch)
    channel = EmailChannel(
        replace(
            _email_config(),
            password="mi clave segura",
            port=587,
            security="starttls",
            retries=0,
        )
    )
    channel._send_smtp(_message())
    assert "login alarma@example.com:mi clave segura" in calls


def test_correo_usa_login_cuando_el_servidor_no_anuncia_plain(monkeypatch) -> None:
    docmd: list[tuple[str, str]] = []

    class SoloLogin:
        esmtp_features: dict[str, str] = {"auth": "LOGIN XOAUTH2"}

        def __init__(
            self, host: str, port: int, timeout: float | None = None, context=None
        ) -> None:
            pass

        def ehlo_or_helo_if_needed(self) -> None:
            pass

        def starttls(self, context=None) -> None:
            pass

        def docmd(self, cmd: str, args: str = "") -> tuple[int, bytes]:
            docmd.append((cmd, args))
            if cmd == "AUTH":
                return 334, b"UGFzc3dvcmQ6"
            return 235, b"2.7.0 Accepted"

        def send_message(self, message: EmailMessage) -> None:
            pass

        def quit(self) -> None:
            pass

    monkeypatch.setattr(smtplib, "SMTP", SoloLogin)
    channel = EmailChannel(replace(_email_config(), port=587, security="starttls", retries=0))
    channel._send_smtp(_message())
    assert docmd[0][0] == "AUTH"
    assert docmd[0][1].startswith("LOGIN ")
    assert docmd[1][0] == "c3VwZXItc2VjcmV0YQ=="  # super-secreta
