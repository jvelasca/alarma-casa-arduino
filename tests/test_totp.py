"""Pruebas del TOTP.

Lo importante no es que el codigo cambie, sino que coincida con el de las apps
de autenticacion: si el algoritmo se desvia, el usuario ve un codigo en el movil
y el PC calcula otro. Por eso se comprueba contra los vectores del RFC 6238.
"""

from __future__ import annotations

import base64

from alarm.services.totp import (
    DIGITS,
    code_at,
    generate_secret,
    normalise_secret,
    provisioning_uri,
    verify_code,
)

#: Secreto de ejemplo del RFC 6238: los numeros 12345678901234567890 en ASCII.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii").rstrip("=")


def test_vectores_del_rfc_6238() -> None:
    # (instante, codigo de 8 cifras) tal como los publica el RFC.
    vectors = (
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    )
    for moment, expected in vectors:
        assert code_at(RFC_SECRET, moment, digits=8) == expected


def test_el_codigo_por_defecto_tiene_seis_cifras() -> None:
    code = code_at(generate_secret(), 1_700_000_000)
    assert len(code) == DIGITS
    assert code.isdigit()


def test_un_codigo_vale_en_su_paso_y_en_los_vecinos() -> None:
    secret = generate_secret()
    moment = 1_700_000_000
    code = code_at(secret, moment)
    assert verify_code(secret, code, moment)
    # 20 s antes y despues sigue dentro de la ventana de ±1 paso.
    assert verify_code(secret, code, moment + 20)
    assert verify_code(secret, code, moment - 20)
    # Un minuto y medio despues ya no.
    assert not verify_code(secret, code, moment + 90)


def test_el_secreto_con_espacios_o_minusculas_vale_igual() -> None:
    secret = generate_secret()
    spaced = " ".join(secret.lower()[i : i + 4] for i in range(0, len(secret), 4))
    assert normalise_secret(spaced) == secret
    code = code_at(secret, 1_700_000_000)
    assert verify_code(spaced, code, 1_700_000_000)


def test_un_codigo_no_se_puede_repetir() -> None:
    secret = generate_secret()
    moment = 1_700_000_000
    code = code_at(secret, moment)
    used: set[int] = set()
    assert verify_code(secret, code, moment, used=used)
    assert not verify_code(secret, code, moment, used=used)


def test_codigos_que_no_son_de_seis_cifras_se_rechazan() -> None:
    secret = generate_secret()
    moment = 1_700_000_000
    assert not verify_code(secret, "12345", moment)
    assert not verify_code(secret, "1234567", moment)
    assert not verify_code(secret, "abcdef", moment)
    assert not verify_code(secret, "", moment)


def test_un_secreto_que_no_es_base32_no_revienta() -> None:
    assert not verify_code("no-es-base32!", "123456", 1_700_000_000)


def test_la_uri_de_alta_lleva_el_secreto_y_el_emisor() -> None:
    secret = generate_secret()
    uri = provisioning_uri(secret, label="alarma-casa", issuer="Alarma")
    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "issuer=Alarma" in uri
    assert "digits=6" in uri
    assert "period=30" in uri


def test_los_secretos_generados_son_distintos_y_largos() -> None:
    secrets = {generate_secret() for _ in range(20)}
    assert len(secrets) == 20
    assert all(len(secret) == 32 for secret in secrets)
