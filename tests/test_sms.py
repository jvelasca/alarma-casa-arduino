"""Pruebas de los canales SMS.

No se toca la red: el transporte HTTP de Twilio se inyecta y la pasarela
correo-a-SMS recibe un doble de `EmailChannel`. Asi las pruebas comprueban el
contrato (que se envia, a quien y con que cuerpo) sin depender de Twilio.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import replace

from alarm.config import SmsConfig
from alarm.services.base import DeliveryResult
from alarm.services.sms_sender import (
    SMS_MAX_CHARS,
    EmailGatewaySmsChannel,
    SmsSwitch,
    TwilioSmsChannel,
    build_sms_channel,
    clip,
    normalize_number,
)


def sms_config(**overrides: object) -> SmsConfig:
    base = SmsConfig(
        enabled=True,
        provider="twilio",
        to=("+34600111222",),
        from_number="+34900000000",
        gateway_domain="sms.example.com",
        sid="AC123",
        token="token-secreto",
        retries=0,
        timeout_s=5.0,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_missing_fields_dice_justo_lo_que_falta() -> None:
    completo = sms_config()
    assert completo.missing_fields == ()
    assert completo.configured is True

    sin_destino = sms_config(to=())
    assert sin_destino.missing_fields == ("destinatarios",)
    assert sin_destino.configured is False

    sin_sid = sms_config(sid="")
    assert sin_sid.missing_fields == ("cuenta SID",)
    assert sin_sid.configured is False

    vacio = sms_config(sid="", token="", from_number="")
    assert vacio.missing_fields == (
        "cuenta SID",
        "token de autenticación",
        "número remitente",
    )


def test_missing_fields_en_la_pasarela() -> None:
    pasarela = sms_config(provider="email_gateway", gateway_domain="")
    assert pasarela.missing_fields == ("dominio de la pasarela",)
    assert pasarela.configured is False
    assert sms_config(provider="email_gateway").configured is True


class FakeTransport:
    """Transporte HTTP de mentira que recuerda lo enviado."""

    def __init__(self, *statuses: int | Exception) -> None:
        self.statuses = list(statuses) or [201]
        self.calls: list[tuple[str, str, dict[str, str], float]] = []

    def __call__(
        self, url: str, data: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, str]:
        self.calls.append((url, data.decode(), dict(headers), timeout))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(status, Exception):
            raise status
        return status, "{}"

    def forms(self) -> list[dict[str, list[str]]]:
        return [urllib.parse.parse_qs(call[1]) for call in self.calls]


class FakeEmail:
    def __init__(self, *, configured: bool = True, ok: bool = True) -> None:
        self.config = replace(sms_config(), provider="email_gateway")
        self._configured = configured
        self._ok = ok
        self.sent: list[tuple[str, str, tuple[str, ...]]] = []

    @property
    def configured(self) -> bool:
        return self._configured

    def send(self, subject: str, body: str, *, to: tuple[str, ...] | None = None) -> DeliveryResult:
        self.sent.append((subject, body, to or ()))
        return DeliveryResult("email", self._ok, "ok" if self._ok else "rebotado")


def test_normaliza_los_numeros_espanoles() -> None:
    assert normalize_number("+34 600 11 22 33") == "+34600112233"
    assert normalize_number("600112233") == "+34600112233"
    assert normalize_number("0034600112233") == "+34600112233"
    assert normalize_number("(600) 11-22-33") == "+34600112233"
    assert normalize_number("") == ""


def test_recorta_a_un_segmento_sin_partir_palabras() -> None:
    corto = "Alarma disparada"
    assert clip(corto) == corto

    largo = "palabra " * 40
    recortado = clip(largo)
    assert len(recortado) <= SMS_MAX_CHARS
    assert recortado.endswith("…")
    assert not recortado[:-1].endswith(" ")


def test_twilio_manda_un_mensaje_por_numero() -> None:
    transport = FakeTransport(201, 201)
    channel = TwilioSmsChannel(sms_config(to=("600111222", "+34600333444")), transport=transport)

    result = channel.send("Alarma", "Salta la alarma")

    assert result.ok is True
    assert "+34600111222" in result.detail
    assert "+34600333444" in result.detail
    forms = transport.forms()
    assert [f["To"][0] for f in forms] == ["+34600111222", "+34600333444"]
    assert forms[0]["From"][0] == "+34900000000"
    assert forms[0]["Body"][0] == "Salta la alarma"
    assert transport.calls[0][0].endswith("/Accounts/AC123/Messages.json")
    assert transport.calls[0][2]["Authorization"].startswith("Basic ")
    assert transport.calls[0][3] == 5.0


def test_twilio_reintenta_antes_de_darse_por_vencido() -> None:
    transport = FakeTransport(500, None, 201)  # type: ignore[arg-type]
    transport.statuses = [500, 201]
    channel = TwilioSmsChannel(sms_config(retries=2), transport=transport)

    result = channel.send("Alarma", "texto")
    assert result.ok is True
    assert len(transport.calls) == 2


def test_twilio_sanea_el_token_en_los_errores() -> None:
    def explota(url: str, data: bytes, headers: dict[str, str], timeout: float) -> tuple[int, str]:
        raise OSError("fallo con token-secreto en la url")

    channel = TwilioSmsChannel(sms_config(), transport=explota)
    result = channel.send("Alarma", "texto")
    assert result.ok is False
    assert "token-secreto" not in result.detail
    assert "***" in result.detail


def test_twilio_sin_credenciales_no_intenta_nada() -> None:
    transport = FakeTransport()
    channel = TwilioSmsChannel(sms_config(token=""), transport=transport)
    result = channel.send("Alarma", "texto")
    assert result.ok is False
    assert "token de autenticación" in result.detail
    assert transport.calls == []


def test_twilio_nombra_los_campos_que_faltan() -> None:
    transport = FakeTransport()
    config = sms_config(sid="", token="", from_number="", to=())
    channel = TwilioSmsChannel(config, transport=transport)
    result = channel.send("Alarma", "texto")
    assert result.ok is False
    assert "destinatarios" in result.detail
    assert transport.calls == []

    config = sms_config(sid="AC123", token="", from_number="", to=("+34600111222",))
    result = TwilioSmsChannel(config, transport=transport).send("Alarma", "texto")
    assert "token de autenticación" in result.detail
    assert "número remitente" in result.detail
    assert "cuenta SID" not in result.detail


def test_twilio_deshabilitado_no_envia() -> None:
    transport = FakeTransport()
    channel = TwilioSmsChannel(sms_config(enabled=False), transport=transport)
    assert channel.send("Alarma", "texto").ok is False
    assert transport.calls == []


def test_twilio_sin_numeros_validos() -> None:
    transport = FakeTransport()
    channel = TwilioSmsChannel(sms_config(to=("",)), transport=transport)
    assert channel.send("Alarma", "texto").ok is False
    assert transport.calls == []


def test_la_pasarela_manda_un_correo_por_numero() -> None:
    email = FakeEmail()
    channel = EmailGatewaySmsChannel(sms_config(provider="email_gateway", to=("600111222",)), email)
    result = channel.send("[Alarma] prueba", "cuerpo")

    assert result.ok is True
    assert len(email.sent) == 1
    subject, body, to = email.sent[0]
    assert subject == "[Alarma] prueba"
    assert body == "cuerpo"
    assert to == ("34600111222@sms.example.com",)


def test_la_pasarela_exige_dominio_y_smtp() -> None:
    email = FakeEmail()
    sin_dominio = EmailGatewaySmsChannel(
        sms_config(provider="email_gateway", gateway_domain=""), email
    )
    assert sin_dominio.send("a", "b").ok is False
    assert email.sent == []

    sin_smtp = EmailGatewaySmsChannel(
        sms_config(provider="email_gateway"), FakeEmail(configured=False)
    )
    assert sin_smtp.send("a", "b").ok is False


def test_el_switch_elige_el_proveedor_activo() -> None:
    twilio = TwilioSmsChannel(sms_config(), transport=FakeTransport())
    gateway = EmailGatewaySmsChannel(sms_config(provider="email_gateway"), FakeEmail())

    switch = SmsSwitch(sms_config(), twilio=twilio, gateway=gateway)
    assert switch.provider is twilio
    assert twilio.enabled is True
    assert gateway.enabled is False

    switch.reconfigure(sms_config(provider="email_gateway"))
    assert switch.provider is gateway
    assert gateway.enabled is True
    assert twilio.enabled is False


def test_el_switch_respeta_los_eventos() -> None:
    switch = SmsSwitch(
        sms_config(on_armed=False, on_triggered=True),
        twilio=TwilioSmsChannel(sms_config(), transport=FakeTransport()),
        gateway=EmailGatewaySmsChannel(sms_config(provider="email_gateway"), FakeEmail()),
    )
    assert switch.enabled_for("triggered") is True
    assert switch.enabled_for("armed") is False
    assert switch.enabled_for("disarmed") is False


def test_desactivar_el_switch_desactiva_el_proveedor() -> None:
    twilio = TwilioSmsChannel(sms_config(), transport=FakeTransport())
    switch = SmsSwitch(
        sms_config(), twilio=twilio, gateway=EmailGatewaySmsChannel(sms_config(), FakeEmail())
    )
    switch.set_enabled(False)
    assert switch.enabled is False
    assert twilio.enabled is False
    assert switch.send("a", "b").ok is False


def test_build_sms_channel_usa_la_pasarela_cuando_toca() -> None:
    email = FakeEmail()
    channel = build_sms_channel(sms_config(provider="email_gateway", to=("600111222",)), email)  # type: ignore[arg-type]
    result = channel.send("Alarma", "cuerpo")
    assert result.ok is True
    assert len(email.sent) == 1
