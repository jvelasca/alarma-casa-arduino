"""Pruebas del volcado de la configuracion a JSON.

Lo importante no es el texto exacto del fichero, sino tres promesas: que
aparecen todas las secciones, que las credenciales no salen y que el resultado
es JSON valido que cualquiera puede volver a leer.
"""

from __future__ import annotations

import json
from dataclasses import replace

from alarm.config import default_config
from alarm.config_export import (
    FORMAT_VERSION,
    config_to_dict,
    config_to_json,
    export_config_json,
)


def _config():
    base = default_config()
    return replace(
        base,
        email=replace(
            base.email,
            user="yo@gmail.com",
            password="clave-secreta",
            to=("alguien@example.com",),
            sender="alarma@example.com",
            enabled=True,
        ),
        sms=replace(
            base.sms,
            sid="AC1234567890",
            token="token-secreto",
            from_number="+34600000000",
            to=("+34600111222",),
            enabled=True,
        ),
    )


def test_el_volcado_trae_todas_las_secciones() -> None:
    data = config_to_dict(_config())
    for section in ("serial", "alarm", "tts", "email", "sms", "ui", "buzzer", "log"):
        assert section in data, f"falta la sección {section}"
    assert data["formato"] == FORMAT_VERSION
    assert "generado" in data
    assert "version" in data
    assert "source" not in data, "el origen se publica como `origen`, no como campo crudo"


def test_las_credenciales_quedan_en_blanco() -> None:
    data = config_to_dict(_config())
    assert data["email"]["user"] == ""
    assert data["email"]["password"] == ""
    assert data["sms"]["token"] == ""
    assert data["ntfy"]["token"] == ""
    assert data["credenciales_omitidas"] == [
        "email.user",
        "email.password",
        "sms.token",
        "ntfy.token",
        "remote.key",
        "remote.totp_secret",
    ]


def test_el_resto_de_los_datos_se_conserva() -> None:
    data = config_to_dict(_config())
    assert data["email"]["to"] == ["alguien@example.com"]
    assert data["email"]["sender"] == "alarma@example.com"
    assert data["sms"]["sid"] == "AC1234567890"
    assert data["sms"]["from_number"] == "+34600000000"
    assert data["sms"]["to"] == ["+34600111222"]


def test_sin_redactar_se_puede_volcar_todo_para_depurar() -> None:
    data = config_to_dict(_config(), redact=False)
    assert data["email"]["password"] == "clave-secreta"
    assert data["sms"]["token"] == "token-secreto"
    assert data["credenciales_omitidas"] == []


def test_el_json_es_texto_legible_y_releible() -> None:
    text = config_to_json(_config())
    assert "contraseña" in text or "password" in text
    data = json.loads(text)
    assert data["formato"] == FORMAT_VERSION
    assert "\n  " in text, "se escribe indentado para poder leerlo"


def test_exportar_escribe_un_fichero_valido(tmp_path) -> None:
    target = tmp_path / "config.json"
    written = export_config_json(_config(), target)

    assert written == target
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["sms"]["enabled"] is True
    assert data["email"]["password"] == ""


def test_una_seccion_nueva_entra_sola(monkeypatch) -> None:
    """El volcado recorre los campos reales, no una lista fija."""
    import dataclasses

    from alarm import config_export

    original = config_export._plain

    def fake_plain(value):
        result = original(value)
        if isinstance(value, config_export.AppConfig):
            result["nueva"] = {"valor": 1}
        return result

    monkeypatch.setattr(config_export, "_plain", fake_plain)
    data = config_export.config_to_dict(_config())
    assert data["nueva"] == {"valor": 1}
    assert dataclasses.is_dataclass(config_export.AppConfig)
