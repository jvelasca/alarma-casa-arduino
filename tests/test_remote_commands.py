"""Pruebas de la gramatica de las ordenes y de la autenticacion.

El objetivo es que teclear «Clave, ARMAR» desde el movil funcione igual que
«armar clave», y que ninguna variante sin credencial valida ejecute nada.
"""

from __future__ import annotations

from dataclasses import replace

from alarm.config import RemoteConfig
from alarm.services.ntfy_commands import (
    CommandAuthorizer,
    StatusInfo,
    format_status,
    normalise_token,
    verb_of,
)
from alarm.services.totp import code_at, generate_secret

SECRET = generate_secret()


def remote(**overrides: object) -> RemoteConfig:
    base = RemoteConfig(
        enabled=True,
        key="clave-de-prueba-1234",
        lockout_failures=5,
        lockout_minutes=15,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def authorizer(**overrides: object) -> CommandAuthorizer:
    moment = 1_700_000_000.0
    return CommandAuthorizer(remote(**overrides), clock=lambda: moment)


# ---------------------------------------------------------------------------
# Normalizacion
# ---------------------------------------------------------------------------


def test_la_normalizacion_quita_acentos_mayusculas_y_puntuacion() -> None:
    assert normalise_token("PÁNICO,") == "panico"
    assert normalise_token("¡Armar!") == "armar"
    assert normalise_token("...DESARMAR...") == "desarmar"
    # El guion y el guion bajo no se tocan: las claves los llevan.
    assert normalise_token("abc-DEF_123") == "abc-def_123"


def test_los_alias_llevan_a_la_misma_accion() -> None:
    assert verb_of("armar") == verb_of("activar") == verb_of("arm") == "arm"
    assert verb_of("desarmar") == verb_of("apagar") == verb_of("disarm") == "disarm"
    assert verb_of("consultar") == verb_of("estado") == verb_of("status") == "status"
    assert verb_of("silenciar") == verb_of("callar") == verb_of("silencio") == "silence"
    assert verb_of("ayuda") == verb_of("help") == verb_of("?") == "help"
    assert verb_of("pánico") == verb_of("panic") == "panic"
    assert verb_of("cualquier cosa") == ""


# ---------------------------------------------------------------------------
# Interpretacion
# ---------------------------------------------------------------------------


def test_la_clave_vale_antes_o_despues_de_la_orden() -> None:
    auth = authorizer()
    for text in ("clave-de-prueba-1234 armar", "armar clave-de-prueba-1234"):
        verdict = auth.evaluate(text)
        assert verdict.accepted, text
        assert verdict.action == "arm"


def test_sin_verbo_no_es_una_orden_y_no_cuenta_como_fallo() -> None:
    auth = authorizer()
    verdict = auth.evaluate("hola que tal")
    assert verdict.action == ""
    assert verdict.is_command is False
    assert auth.blocked is False


def test_una_clave_erronea_rechaza() -> None:
    auth = authorizer()
    verdict = auth.evaluate("clave-mala armar")
    assert not verdict.accepted
    assert "clave" in verdict.reason


def test_una_clave_correcta_limpia_los_fallos() -> None:
    auth = authorizer()
    for _ in range(3):
        auth.evaluate("mala armar")
    assert auth.evaluate("clave-de-prueba-1234 armar").accepted
    # Tras acertar, el contador vuelve a cero: no se bloquea con 2 fallos mas.
    for _ in range(3):
        auth.evaluate("mala armar")
    assert not auth.blocked


def test_una_orden_desactivada_se_rechaza_aunque_la_clave_sea_buena() -> None:
    auth = authorizer(allow_arm=False)
    verdict = auth.evaluate("clave-de-prueba-1234 armar")
    assert not verdict.accepted
    assert "desactivada" in verdict.reason


def test_la_ayuda_se_responde_sin_credencial() -> None:
    """`ayuda` es informativa: no cambia nada y no puede exigir clave.

    Es la orden que se teclea justo cuando algo no funciona; pedir la clave
    para leerla dejaria al usuario sin la unica ayuda disponible.
    """
    auth = authorizer()
    for text in ("ayuda", "help", "?", "AYUDA"):
        verdict = auth.evaluate(text)
        assert verdict.accepted, text
        assert verdict.action == "help"


def test_la_ayuda_funciona_con_todas_las_ordenes_desactivadas() -> None:
    auth = authorizer(
        allow_arm=False,
        allow_disarm=False,
        allow_status=False,
        allow_silence=False,
        allow_panic=False,
    )
    assert auth.evaluate("ayuda").accepted
    assert not auth.evaluate("armar").accepted


def test_la_ayuda_no_queda_bloqueada_por_los_intentos_fallidos() -> None:
    auth = authorizer(lockout_failures=2)
    auth.evaluate("mala armar")
    auth.evaluate("mala armar")
    assert auth.blocked

    verdict = auth.evaluate("ayuda")
    assert verdict.accepted
    assert verdict.blocked is False
    # Consultar la ayuda no reinicia el bloqueo ni alarga la espera.
    assert auth.blocked


def test_la_ayuda_no_cuenta_como_intento_fallido() -> None:
    auth = authorizer(lockout_failures=3)
    for _ in range(10):
        assert auth.evaluate("ayuda").accepted
    assert not auth.blocked


def test_la_ayuda_no_aparece_como_orden_configurable() -> None:
    config = remote()
    assert config.allows_action("help") is True
    assert "help" not in config.allows
    # Sigue sin poder desactivarse: la lista vacia no la apaga.
    apagado = remote(
        allow_arm=False,
        allow_disarm=False,
        allow_status=False,
        allow_silence=False,
        allow_panic=False,
    )
    assert apagado.allows == ()
    assert apagado.allows_action("help") is True


def test_el_bloqueo_salta_al_llegar_al_limite_y_avisa_una_sola_vez() -> None:
    auth = authorizer(lockout_failures=3)
    assert not auth.evaluate("mala armar").locked_now
    assert not auth.evaluate("mala armar").locked_now
    third = auth.evaluate("mala armar")
    assert third.locked_now is True
    assert auth.blocked is True
    # Ya bloqueado: ni siquiera la clave buena pasa.
    blocked = auth.evaluate("clave-de-prueba-1234 armar")
    assert blocked.blocked
    assert blocked.locked_now is False


def test_sin_bloqueo_configurado_no_se_bloquea_nunca() -> None:
    auth = authorizer(lockout_failures=0)
    for _ in range(20):
        assert not auth.evaluate("mala armar").locked_now
    assert not auth.blocked


def test_el_bloqueo_caduca() -> None:
    moment = 1_700_000_000.0
    auth = CommandAuthorizer(remote(lockout_failures=2, lockout_minutes=1), clock=lambda: moment)
    auth.evaluate("mala armar")
    auth.evaluate("mala armar")
    assert auth.blocked
    moment += 61
    assert not auth.blocked
    assert auth.evaluate("clave-de-prueba-1234 armar").accepted


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


def test_con_totp_solo_valen_los_seis_digitos() -> None:
    auth = authorizer(totp_secret=SECRET)
    code = code_at(SECRET, 1_700_000_000)
    assert auth.evaluate(f"{code} armar").accepted
    # La clave deja de aceptarse aunque siga configurada.
    assert not auth.evaluate("clave-de-prueba-1234 armar").accepted


def test_un_codigo_totp_caducado_no_vale() -> None:
    auth = authorizer(totp_secret=SECRET)
    old = code_at(SECRET, 1_700_000_000 - 3600)
    assert not auth.evaluate(f"{old} armar").accepted


def test_el_mismo_codigo_no_vale_dos_veces() -> None:
    auth = authorizer(totp_secret=SECRET)
    code = code_at(SECRET, 1_700_000_000)
    assert auth.evaluate(f"{code} armar").accepted
    assert not auth.evaluate(f"{code} desarmar").accepted


def test_sin_credenciales_configuradas_todo_se_rechaza() -> None:
    auth = authorizer(key="")
    verdict = auth.evaluate("armar")
    assert not verdict.accepted
    assert "clave" in verdict.reason


# ---------------------------------------------------------------------------
# Respuesta de estado
# ---------------------------------------------------------------------------


def test_el_estado_resume_lo_que_se_quiere_saber() -> None:
    info = StatusInfo(
        state_label="Armada",
        link="COM4 · Conectado",
        sensor="listo",
        armed=True,
        siren_s=42.0,
        closing_s=12.0,
        delay_kind="entry",
        delays="salida 45 s · entrada 30 s",
        channels=("móvil",),
        events=("08:12 · Alarma: armada",),
        now="08:15:00",
    )
    text = format_status(info)
    assert "Estado: Armada" in text
    assert "Cuenta atrás (entry): quedan 12 s" in text
    assert "Sirena: quedan 42 s" in text
    assert "COM4 · Conectado" in text
    assert "Retardos: salida 45 s · entrada 30 s" in text
    assert "Canales: móvil" in text
    assert "08:12 · Alarma: armada" in text
    assert "Hora: 08:15:00" in text


def test_el_estado_minimo_no_miente_ni_inventa() -> None:
    text = format_status(StatusInfo(state_label="Desarmada", link=""))
    assert "Estado: Desarmada" in text
    assert "Enlace: sin datos" in text
    assert "Canales: ninguno activo" in text
    assert "Sirena" not in text


def test_las_ordenes_por_defecto_no_incluyen_panico() -> None:
    config = remote()
    assert "arm" in config.allows
    assert "panic" not in config.allows
    assert "panic" in remote(allow_panic=True).allows


def test_el_tema_de_mando_se_deriva_del_de_avisos() -> None:
    from alarm.config import NtfyConfig

    config = NtfyConfig(topic="casa-abc")
    assert remote().topic_for(config) == "casa-abc-mando"
    assert remote(topic="otro-tema").topic_for(config) == "otro-tema"
