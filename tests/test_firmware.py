"""Pruebas del contrato con el firmware real.

El codec se prueba con lineas construidas a mano en `test_codec.py`, pero eso
no detecta que el sketch emita JSON mal formado: el `hello` llevaba tiempo
saliendo con `}],,` (una coma al final de un `print` y otra al principio del
siguiente) y la aplicacion rechazaba el saludo de la placa de verdad. Aqui se
fija la linea capturada del hardware y se revisa el emisor del sketch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from alarm.protocol.codec import MAX_LINE, decode_line
from alarm.protocol.messages import Heartbeat, Hello, Ready

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware" / "mega-pir" / "mega-pir.ino"

#: Linea tal cual la escupio un Mega 2560 por COM3 a 115200 baudios.
HELLO_REAL = (
    '{"t":"hello","proto":1,"dev":"mega-pir","fw":"1.4.1","boot":0,'
    '"zones":[{"id":1,"pin":2,"name":"pir"}],'
    '"caps":["hello","set","replay","calibrate","beep"],'
    '"cfg":{"warmup_ms":60000,"debounce_ms":50,"hb_ms":2000,'
    '"calib_window_ms":5000,"buzzer_pin":11,"buzzer":1,"armed":0}}'
)

#: Latido durante el calentamiento: llega cada 5 s y solo trae `warmup`.
HB_REAL = '{"t":"hb","up":5000,"lvl":0,"raw":0,"buf":0,"lost":0,"calib":0,"warmup":1}'

#: Latido normal, ya terminado el calentamiento.
HB_REAL_LISTO = '{"t":"hb","up":65000,"lvl":0,"raw":3,"buf":0,"lost":0,"calib":0}'

READY_REAL = '{"t":"ready","warmup_ms":60000}'


def test_la_linea_hello_del_hardware_se_decodifica() -> None:
    message = decode_line(HELLO_REAL)

    assert isinstance(message, Hello)
    assert message.dev == "mega-pir"
    assert message.fw == "1.4.1"
    assert [(zone.pin, zone.name) for zone in message.zones] == [(2, "pir")]
    assert "beep" in message.caps
    assert message.cfg["buzzer_pin"] == 11
    assert message.cfg["warmup_ms"] == 60000


def test_la_linea_cabe_en_el_limite_del_buffer() -> None:
    assert len(HELLO_REAL) < MAX_LINE


def test_los_latidos_reales_se_decodifican() -> None:
    durante = decode_line(HB_REAL)
    listo = decode_line(HB_REAL_LISTO)

    assert isinstance(durante, Heartbeat)
    assert isinstance(listo, Heartbeat)
    assert durante.warming_up is True
    assert durante.up_ms == 5000
    assert listo.warming_up is False
    assert listo.raw_edges == 3


def test_el_ready_real_se_decodifica() -> None:
    message = decode_line(READY_REAL)

    assert isinstance(message, Ready)
    assert message.warmup_ms == 60000


# -- El emisor del sketch ---------------------------------------------------


def _cuerpo(name: str) -> str:
    """Devuelve el cuerpo de `tipo name(...)`, sin las llaves exteriores."""
    source = FIRMWARE.read_text(encoding="utf-8")
    match = re.search(rf"\w+ {name}\([^)]*\) \{{(.*?)\n\}}", source, re.S)
    assert match, f"no se encuentra la funcion {name} en el sketch"
    return match.group(1)


def _assembled_json(body: str) -> str:
    """Reconstruye lo que el sketch imprime, con `0` donde va un valor.

    Cada `Serial.print(F("..."))` aporta su literal y cada `Serial.print(x)`
    con una variable aporta un cero. Asi las comas de dos literales que se
    imprimen seguidos quedan juntas y se ven, que es justo el fallo.
    """
    pieces: list[str] = []
    for match in re.finditer(r"Serial\.print(?:ln)?\((.*?)\);", body, re.S):
        argument = match.group(1).strip()
        literal = re.fullmatch(r'F\("((?:[^"\\]|\\.)*)"\)', argument, re.S)
        if literal is None:
            pieces.append("0")
            continue
        pieces.append(literal.group(1).replace('\\"', '"').replace("\\n", "\n"))
    return "".join(pieces)


def test_el_hello_del_sketch_es_json_valido() -> None:
    """Regresion del `}],,` que hacia ilegible el saludo de la placa."""
    text = _assembled_json(_cuerpo("emitHello"))

    assert ",," not in text, f"coma doble en el hello del sketch: {text}"
    data = json.loads(text)
    assert data["t"] == "hello"
    assert data["proto"] == 1
    assert data["cfg"].keys() >= {"warmup_ms", "buzzer_pin", "buzzer", "armed", "wdt_rst"}
    assert data["zones"] == [{"id": 1, "pin": 0, "name": "pir"}]
    assert "boot_id" in data, "el hello tiene que anunciar la sesion de arranque"


# -- Identidad de arranque en EEPROM ---------------------------------------


def test_el_sketch_guarda_el_boot_id_en_dos_ranuras_con_crc() -> None:
    """H-02: sin identidad de sesion el PC no puede deduplicar.

    El contador va en EEPROM para sobrevivir al reinicio, en dos ranuras
    alternas con magic y CRC16 para que un corte a mitad de escritura no lo
    destruya.
    """
    source = FIRMWARE.read_text(encoding="utf-8")

    assert "#include <EEPROM.h>" in source
    assert "BOOT_SLOT_A" in source and "BOOT_SLOT_B" in source, "faltan las dos ranuras"
    assert "BOOT_MAGIC" in source, "falta el magic que valida la ranura"
    assert "crc16Ccitt" in source, "falta el CRC de la ranura"
    assert "EEPROM.update" in source, "se escribe la EEPROM sin `update`"
    assert "uint32_t nextBootId()" in source, "falta el incremento del contador"


def test_la_eeprom_solo_se_toca_una_vez_por_arranque() -> None:
    """Escribir EEPROM en `loop()` la gastaria en semanas (100 000 ciclos)."""
    setup = _cuerpo("setup")
    loop = _cuerpo("loop")

    assert "nextBootId()" in setup
    assert setup.index("nextBootId()") < setup.index("emitHello()"), (
        "el hello tiene que salir ya con el boot_id definitivo"
    )
    assert "EEPROM" not in loop, "ninguna escritura de EEPROM dentro de loop()"


def test_una_ranura_solo_vale_con_magic_y_crc_correctos() -> None:
    """Un corte a mitad de escritura deja la ranura invalida, no un valor falso.

    `readBootSlot` tiene que comprobar el magic *y* el CRC16 antes de aceptar
    los cuatro bytes del contador. Aceptar el magic solo convertiria una
    escritura a medias en un `boot_id` basura que el PC tomaria por bueno.
    """
    read = _cuerpo("readBootSlot")
    write = _cuerpo("writeBootSlot")

    assert "magic != BOOT_MAGIC" in read
    assert "crc16Ccitt(raw, 6) != stored" in read, "el CRC se lee y se compara"
    assert "raw[2] | ((uint32_t)raw[3] << 8)" in read, "el contador es de 32 bits"
    assert "crc16Ccitt(raw, 6)" in write, "al escribir se sella el CRC"
    assert "EEPROM.update" in write
    assert "EEPROM.write" not in write, "`write` gasta la celda aunque el valor no cambie"


def test_el_contador_de_arranque_elige_la_ranura_correcta() -> None:
    """Ping-pong: se toma el mayor valido y se escribe en la *otra* ranura.

    Asi un corte mientras se graba no destruye el ultimo contador conocido: la
    ranura vieja sigue intacta y el siguiente arranque la lee.
    """
    source = FIRMWARE.read_text(encoding="utf-8")
    body = _cuerpo("nextBootId")

    assert source.count("BOOT_SLOT_A = 0x") == 1
    assert "BOOT_SLOT_B = 0x10" in source, "las ranuras no pueden solaparse"
    assert "readBootSlot(BOOT_SLOT_A" in body and "readBootSlot(BOOT_SLOT_B" in body
    assert "if (a >= b)" in body, "con las dos validas manda la mayor"
    assert "last = b" in body, "si solo vale B se parte de B"
    assert "uint32_t next = last + 1;" in body, "32 bits: el contador no se corta en 16"
    # Con las dos ranuras validas se toma la mayor y se escribe en la otra.
    assert "if (a >= b) { last = a; target = BOOT_SLOT_B; }" in body
    assert "else        { last = b; target = BOOT_SLOT_A; }" in body
    # Y con una sola valida, tambien en la contraria.
    assert "} else if (okA) {" in body and "} else if (okB) {" in body
    assert "writeBootSlot(target, next)" in body
    assert body.index("writeBootSlot(target, next)") < body.index("return next")


def test_el_boot_id_cero_no_es_el_de_un_firmware_antiguo() -> None:
    """El PC usa `None` para firmware sin identidad; 0 es un valor legitimo."""
    from alarm.protocol.codec import decode_line
    from alarm.protocol.messages import Hello

    sin_id = decode_line(
        '{"t":"hello","proto":1,"dev":"mega-pir","fw":"1.6.0","boot":0,"zones":[],"cfg":{}}'
    )
    con_cero = decode_line(
        '{"t":"hello","proto":1,"dev":"mega-pir","fw":"1.7.0","boot":0,"boot_id":0,'
        '"zones":[],"cfg":{}}'
    )

    assert isinstance(sin_id, Hello) and isinstance(con_cero, Hello)
    assert sin_id.boot_id is None
    assert con_cero.boot_id == 0


def test_la_version_del_firmware_anuncia_el_cambio() -> None:
    source = FIRMWARE.read_text(encoding="utf-8")
    assert 'FW_VERSION[] = "1.7.0"' in source


def test_el_pin_del_zumbador_del_sketch_coincide_con_la_configuracion() -> None:
    """El TOML es informativo, asi que ambos tienen que decir lo mismo."""
    from alarm.config import default_config

    source = FIRMWARE.read_text(encoding="utf-8")
    match = re.search(r"const uint8_t PIN_BUZZER = (\d+);", source)
    assert match, "no se encuentra PIN_BUZZER en el sketch"
    pin = int(match.group(1))

    assert pin == default_config().buzzer.pin, (
        f"el firmware usa D{pin} y la configuración dice {default_config().buzzer.pin}"
    )


@pytest.mark.parametrize("name", ["emitHeartbeat", "emitEvent", "emitCalibEdge"])
def test_los_demas_emisores_no_llevan_comas_dobles(name: str) -> None:
    text = _assembled_json(_cuerpo(name))

    assert ",," not in text, f"coma doble en {name}: {text}"


def test_el_sketch_entiende_los_patrones_que_ofrece_la_ventana() -> None:
    """La ventana del zumbador no debe ofrecer nombres que la placa ignore."""
    from alarm.ui.dialogs.buzzer import PATTERNS

    source = FIRMWARE.read_text(encoding="utf-8")
    match = re.search(r"bool beeperByName\([^)]*\) \{(.*?)\n\}", source, re.S)
    assert match, "no se encuentra beeperByName en el sketch"
    body = match.group(1)

    for name, title, _hint in PATTERNS:
        assert f'"{name}"' in body, f"la ventana ofrece «{title}» y el sketch no lo conoce"


def test_el_pitido_de_prueba_puede_forzar_el_calentamiento() -> None:
    """Regresion: con el PIR calentando, «Probar zumbador» no sonaba.

    El firmware solo pitaba con `g_warmupDone`, asi que recien conectada la
    placa cualquier prueba era silencio y parecia un cableado mal puesto.
    """
    source = FIRMWARE.read_text(encoding="utf-8")

    assert 'strcmp(kv, "force")' in source, "el comando beep debe aceptar force=1"
    assert 'strcmp(kv, "freq")' in source, "el tono continuo necesita frec=Hz"
    assert "(!g_warmupDone && !force)" in source, "la puerta del calentamiento debe respetar force"

    calls = re.findall(r"beeperStart\(([^\n]*)\);", source)
    assert calls, "no se encuentran llamadas a beeperStart"
    for args in calls:
        assert args.count(",") == 4, f"a beeperStart({args}) le falta el permiso `force`"


def test_el_tono_continuo_se_apaga_solo() -> None:
    """Un tono sin final dejaria el piezo sonando hasta desenchufar."""
    source = FIRMWARE.read_text(encoding="utf-8")
    match = re.search(r"void beeperTick\(uint32_t now\) \{(.*?)\n\}", source, re.S)
    assert match, "no se encuentra beeperTick en el sketch"

    assert "g_toneActive" in match.group(1), "beeperTick debe vencer el tono continuo"


def test_el_sketch_genera_las_cadencias_de_los_retardos() -> None:
    """Los retardos pitan en la placa, no a golpe de comando desde el PC."""
    source = FIRMWARE.read_text(encoding="utf-8")

    assert re.search(r"void countdownTick\(uint32_t now\)", source), "falta la cuenta atras"
    tick = re.search(r"void beeperTick\(uint32_t now\) \{(.*?)\n\}", source, re.S)
    assert tick and "countdownTick(now)" in tick.group(1)

    # La cadencia se acelera: el intervalo baja del valor inicial al final.
    assert "g_cdFromMs - (uint16_t)" in source
    assert "span * done / g_cdTotalMs" in source

    names = re.search(r"bool beeperByName\([^)]*\) \{(.*?)\n\}", source, re.S)
    assert names, "no se encuentra beeperByName"
    for name in ("exit", "entry", "stop"):
        assert f'"{name}"' in names.group(1), f"el sketch no entiende el aviso {name!r}"

    # La parada corta tambien la cuenta atras, no solo los patrones.
    stop = re.search(r"void beeperStop\(\) \{(.*?)\n\}", source, re.S)
    assert stop and "g_cdActive" in stop.group(1)

    # Un patron explicito manda sobre la cadencia en curso: el PC acaba de
    # pedir otra cosa y dejar el reloj viejo pitando seria contradecirlo.
    assert "g_cdActive = false" in names.group(1)
    assert 'strcmp(name, "exit") != 0' in names.group(1)


# -- Lo que un auditor mira primero -----------------------------------------


def _sin_comentarios(texto: str) -> str:
    """Quita comentarios: el sketch habla de `delay()` justo para decir que no lo usa."""
    sin_bloque = re.sub(r"/\*.*?\*/", "", texto, flags=re.S)
    return re.sub(r"//[^\n]*", "", sin_bloque)


def test_el_sketch_alimenta_el_watchdog() -> None:
    """Un bucle colgado tiene que reiniciar la placa, no dejarla muda.

    El documento de premisas prometia un watchdog de 8 s desde F1 y el sketch no
    lo tenia: un bloqueo dejaba la placa sin latido hasta que alguien la
    desenchufaba, que es justo lo que un sistema de alarma no puede permitirse.
    """
    source = FIRMWARE.read_text(encoding="utf-8")
    setup = _cuerpo("setup")
    loop = _cuerpo("loop")

    assert "#include <avr/wdt.h>" in source
    assert "wdt_enable(WDTO_8S)" in source, "el watchdog debe armarse a 8 s"

    # La receta contra el bucle de reinicios: leer MCUSR, limpiarlo y desactivar
    # el perro antes de nada, y solo armarlo al final del arranque.
    assert "MCUSR = 0" in setup
    assert "wdt_disable()" in setup
    assert "g_wdtReset" in setup, "el motivo del reinicio tiene que guardarse"
    assert setup.index("MCUSR = 0") < setup.index("wdt_enable"), "limpiar MCUSR va antes de armar"
    assert setup.index("emitHello()") < setup.index("wdt_enable"), "armar despues del hello"

    # Y alimentarlo en cada vuelta del bucle, como primera sentencia.
    cuerpo = _sin_comentarios(loop).strip()
    assert cuerpo.startswith("wdt_reset();"), cuerpo[:60]


def test_el_sketch_no_bloquea_ni_reserva_memoria() -> None:
    """Ni `delay()` ni memoria dinamica: un Mega encendido meses no lo perdona."""
    codigo = _sin_comentarios(FIRMWARE.read_text(encoding="utf-8"))

    prohibidos = {
        r"\bdelay\s*\(": "delay() bloquea el bucle y deja de atender el enlace",
        r"\bdelayMicroseconds\s*\(": "delayMicroseconds() bloquea el bucle",
        r"\bString\b": "String fragmenta los 8 KB de RAM",
        r"\bmalloc\s*\(": "malloc fragmenta los 8 KB de RAM",
        r"\bnew\b": "new fragmenta los 8 KB de RAM",
    }
    for patron, motivo in prohibidos.items():
        assert not re.search(patron, codigo), motivo


def test_el_buffer_y_la_linea_coinciden_con_el_protocolo() -> None:
    """Los numeros del sketch y los del documento tienen que ser los mismos.

    El documento de premisas llego a decir 32 eventos y 256 bytes cuando el
    sketch tenia 16 y 96. Aqui se leen del codigo y se comprueba que el
    protocolo publica esos, para que no vuelvan a separarse.
    """
    source = FIRMWARE.read_text(encoding="utf-8")
    evt = re.search(r"const uint8_t EVT_CAP = (\d+);", source)
    cmd = re.search(r"const uint8_t CMD_CAP = (\d+);", source)
    assert evt, "no se encuentra EVT_CAP en el sketch"
    assert cmd, "no se encuentra CMD_CAP en el sketch"

    protocolo = (FIRMWARE.parents[2] / "doc" / "02-PROTOCOLO.md").read_text(encoding="utf-8")
    assert f"({evt.group(1)} posiciones)" in protocolo, (
        f"el sketch guarda {evt.group(1)} eventos y el protocolo dice otra cosa"
    )
    assert f"a {cmd.group(1)} en el downlink" in protocolo, (
        f"el sketch corta a {cmd.group(1)} bytes y el protocolo dice otra cosa"
    )
