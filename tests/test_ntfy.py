"""Pruebas del canal ntfy.

No se toca la red: el transporte HTTP se inyecta y recuerda lo enviado, igual
que en las pruebas de SMS. Asi se comprueba el contrato (que se publica, donde y
con que cuerpo) sin depender del servidor publico.
"""

from __future__ import annotations

import json
from dataclasses import replace

from alarm.config import NtfyConfig, normalize_ntfy_server, valid_ntfy_topic
from alarm.services.ntfy_sender import (
    MAX_MESSAGE_CHARS,
    NtfyChannel,
    build_title,
    clip_message,
)


def ntfy_config(**overrides: object) -> NtfyConfig:
    base = NtfyConfig(
        enabled=True,
        server="https://ntfy.sh",
        topic="alarma-casa-7f92k5x9",
        retries=0,
        timeout_s=5.0,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class FakeTransport:
    """Transporte HTTP de mentira que recuerda lo enviado."""

    def __init__(self, *statuses: int | Exception) -> None:
        self.statuses = list(statuses) or [200]
        self.calls: list[tuple[str, str, dict[str, str], float]] = []

    def __call__(
        self, url: str, data: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, str]:
        self.calls.append((url, data.decode(), dict(headers), timeout))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(status, Exception):
            raise status
        return status, "{}"

    def payloads(self) -> list[dict[str, object]]:
        return [json.loads(call[1]) for call in self.calls]


# ---------------------------------------------------------------------------
# Validacion de servidor y tema
# ---------------------------------------------------------------------------


def test_valida_el_tema() -> None:
    assert valid_ntfy_topic("alarma-casa-7f92k5x9") is True
    assert valid_ntfy_topic("_guion_bajo") is True
    assert valid_ntfy_topic("") is False
    assert valid_ntfy_topic("con espacio") is False
    assert valid_ntfy_topic("con/punto") is False
    assert valid_ntfy_topic("a" * 65) is False


def test_normaliza_el_servidor() -> None:
    assert normalize_ntfy_server("https://ntfy.sh/") == "https://ntfy.sh"
    assert normalize_ntfy_server("ntfy.sh") == "https://ntfy.sh"
    assert normalize_ntfy_server(" http://192.168.1.10:8080 ") == "http://192.168.1.10:8080"
    assert normalize_ntfy_server("") == ""
    assert normalize_ntfy_server("ftp://ntfy.sh") == ""
    assert normalize_ntfy_server("https://") == ""


def test_missing_fields_dice_justo_lo_que_falta() -> None:
    assert ntfy_config().missing_fields == ()
    assert ntfy_config().configured is True

    assert ntfy_config(topic="").missing_fields == ("tema",)
    assert ntfy_config(topic="con espacio").missing_fields == ("tema",)
    assert ntfy_config(server="").missing_fields == ("servidor",)
    assert ntfy_config(server="", topic="").missing_fields == ("servidor", "tema")


def test_el_token_no_hace_falta_en_el_servidor_publico() -> None:
    config = ntfy_config()
    assert config.token == ""
    assert config.configured is True


def test_reconoce_cuando_publica_en_ntfy_sh() -> None:
    assert ntfy_config().uses_public_server is True
    assert ntfy_config(server="ntfy.sh/").uses_public_server is True
    assert ntfy_config(server="https://ntfy.midominio.com").uses_public_server is False


def test_prioridad_por_causa() -> None:
    config = ntfy_config()
    assert config.priority_for("triggered") == "urgent"
    assert config.priority_for("device_lost") == "high"
    assert config.priority_for("armed") == "default"
    assert config.priority_for("disarmed") == "default"
    assert config.priority_for("otra") == "default"


def test_una_prioridad_invalida_cae_a_normal() -> None:
    config = ntfy_config(priority_triggered="altisima")
    assert config.priority_for("triggered") == "default"


# ---------------------------------------------------------------------------
# Publicacion
# ---------------------------------------------------------------------------


def test_publica_un_json_con_tema_titulo_y_prioridad() -> None:
    transport = FakeTransport(200)
    channel = NtfyChannel(ntfy_config(), transport=transport)

    result = channel.send("Asunto del correo", "Salta la alarma", cause="triggered")

    assert result.ok is True
    assert "alarma-casa-7f92k5x9" in result.detail
    url, _, headers, timeout = transport.calls[0]
    assert url == "https://ntfy.sh/"
    assert headers["Content-Type"] == "application/json"
    assert "Authorization" not in headers
    assert timeout == 5.0
    payload = transport.payloads()[0]
    assert payload["topic"] == "alarma-casa-7f92k5x9"
    assert payload["title"] == "[Alarma] DISPARADA"
    assert payload["message"] == "Salta la alarma"
    assert payload["priority"] == 5
    assert payload["tags"] == ["rotating_light"]


def test_prioridades_y_etiquetas_de_cada_evento() -> None:
    transport = FakeTransport(200, 200, 200)
    channel = NtfyChannel(ntfy_config(), transport=transport)

    channel.send("x", "y", cause="device_lost")
    channel.send("x", "y", cause="armed")
    channel.send("x", "y", cause="disarmed")

    first, second, third = transport.payloads()
    assert first["priority"] == 4
    assert first["tags"] == ["warning"]
    assert second["priority"] == 3
    assert second["tags"] == ["lock"]
    assert third["priority"] == 3
    assert third["tags"] == ["unlock"]


def test_una_prueba_sin_causa_usa_prioridad_normal() -> None:
    transport = FakeTransport(200)
    channel = NtfyChannel(ntfy_config(), transport=transport)

    assert channel.send("Prueba", "texto").ok is True
    payload = transport.payloads()[0]
    assert payload["priority"] == 3
    assert "tags" not in payload


def test_manda_el_token_como_bearer() -> None:
    transport = FakeTransport(200)
    channel = NtfyChannel(ntfy_config(token="tk_secreto"), transport=transport)

    assert channel.send("x", "y").ok is True
    assert transport.calls[0][2]["Authorization"] == "Bearer tk_secreto"


def test_el_servidor_pierde_la_barra_final() -> None:
    transport = FakeTransport(200)
    channel = NtfyChannel(ntfy_config(server="https://ntfy.sh/"), transport=transport)

    channel.send("x", "y")
    assert transport.calls[0][0] == "https://ntfy.sh/"


def test_canal_deshabilitado_no_envia() -> None:
    transport = FakeTransport(200)
    channel = NtfyChannel(ntfy_config(enabled=False), transport=transport)

    result = channel.send("x", "y")
    assert result.ok is False
    assert "deshabilitado" in result.detail
    assert transport.calls == []


def test_sin_datos_no_llama_al_servidor() -> None:
    transport = FakeTransport(200)
    channel = NtfyChannel(ntfy_config(topic=""), transport=transport)

    result = channel.send("x", "y")
    assert result.ok is False
    assert "tema" in result.detail
    assert transport.calls == []


def test_reintenta_ante_un_fallo_de_red() -> None:
    transport = FakeTransport(500, 200)
    channel = NtfyChannel(ntfy_config(retries=1), transport=transport)

    assert channel.send("x", "y").ok is True
    assert len(transport.calls) == 2


def test_un_400_no_se_reintenta() -> None:
    transport = FakeTransport(401, 200)
    channel = NtfyChannel(ntfy_config(retries=3), transport=transport)

    result = channel.send("x", "y")
    assert result.ok is False
    assert "401" in result.detail
    assert len(transport.calls) == 1


def test_un_429_si_se_reintenta() -> None:
    transport = FakeTransport(429, 200)
    channel = NtfyChannel(ntfy_config(retries=1), transport=transport)

    assert channel.send("x", "y").ok is True
    assert len(transport.calls) == 2


def test_el_token_no_acaba_en_el_error() -> None:
    transport = FakeTransport(RuntimeError("fallo con Bearer tk_secreto en la peticion"))
    channel = NtfyChannel(ntfy_config(token="tk_secreto"), transport=transport)

    result = channel.send("x", "y")
    assert result.ok is False
    assert "tk_secreto" not in result.detail
    assert "***" in result.detail


# ---------------------------------------------------------------------------
# Texto
# ---------------------------------------------------------------------------


def test_recorta_el_mensaje_al_limite_del_servidor() -> None:
    corto = "Alarma disparada"
    assert clip_message(corto) == corto

    recortado = clip_message("x" * (MAX_MESSAGE_CHARS + 100))
    assert len(recortado) == MAX_MESSAGE_CHARS
    assert recortado.endswith("…")


def test_el_titulo_usa_la_palabra_del_evento() -> None:
    assert build_title(ntfy_config(), "triggered", "") == "[Alarma] DISPARADA"
    assert build_title(ntfy_config(), "device_lost", "") == "[Alarma] sin sensor"


def test_una_plantilla_sin_evento_se_respeta() -> None:
    config = ntfy_config(title="Casa")
    assert build_title(config, "triggered", "") == "Casa"


def test_una_plantilla_rota_no_rompe_el_aviso() -> None:
    config = ntfy_config(title="Alarma {desconocido}")
    assert build_title(config, "triggered", "") == "Alarma {desconocido}"


def test_set_enabled_y_reconfigure() -> None:
    config = ntfy_config(enabled=False)
    channel = NtfyChannel(config)
    assert channel.enabled is False

    channel.set_enabled(True)
    assert channel.enabled is False, "la configuracion sigue apagada"

    channel.reconfigure(ntfy_config(enabled=True))
    assert channel.enabled is True
    assert channel.configured is True
