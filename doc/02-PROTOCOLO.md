# Protocolo serie v1

Referencia del enlace entre el Arduino Mega 2560 y el PC. Implementado en
`firmware/mega-pir/mega-pir.ino` y en `src/alarm/protocol/`.

---

## 1. Capa fisica

| Parametro | Valor |
|---|---|
| Puerto | USB serie (CH340 o ATmega16U2) |
| Velocidad | 115200 baudios, 8N1, sin control de flujo |
| Codificacion | UTF-8, solo ASCII en la practica |
| Delimitador | `\n` (se tolera `\r`) |
| Longitud maxima | 512 bytes por linea; el firmware corta a 96 en el downlink |

---

## 2. Direcciones y por que son distintas

| Direccion | Formato |
|---|---|
| Uplink (Arduino -> PC) | NDJSON: un objeto JSON por linea |
| Downlink (PC -> Arduino) | Texto tokenizado: `cmd <id> <op> [k=v ...]` |

Es una asimetria deliberada (ADR-011). El PC necesita un formato auto-descriptivo
para crecer sin tocar el parser; el ATmega2560 tiene 8 KB de RAM y un parser
JSON completo cuesta mas de lo que aporta para cinco comandos. El uplink lo
genera el dispositivo con `Serial.print`, que es barato; el downlink lo parsea
con `strtok_r`, que tambien es barato.

---

## 3. Uplink (dispositivo -> PC)

Todos los mensajes llevan el campo `t` con el tipo.

### 3.1 `hello`

Se emite al arrancar y cada vez que el PC lo pide con `cmd <id> hello`.

```json
{"t":"hello","proto":1,"dev":"mega-pir","fw":"1.6.0","boot":0,
 "zones":[{"id":1,"pin":2,"name":"pir"}],
 "caps":["hello","set","replay","calibrate","beep"],
 "cfg":{"warmup_ms":60000,"debounce_ms":50,"hb_ms":2000,"calib_window_ms":5000,
        "buzzer_pin":11,"buzzer":1,"armed":0,"wdt_rst":0}}
```

`boot` es el `millis()` del arranque. Como vale ~0 siempre, sirve para detectar
un reinicio: si cambia respecto al anterior, la placa se reinicio.

`cfg.buzzer` y `cfg.armed` reflejan el estado en RAM del dispositivo, no la
configuracion del PC: al reiniciar por DTR vuelven a `1` y `0`. El PC reenvia
los suyos al recibir `hello`.

`cfg.buzzer_pin` es el pin real, el de `PIN_BUZZER` en el sketch; el
`[buzzer].pin` del TOML es solo informativo y no viaja a la placa.

`cfg.wdt_rst` vale `1` si el arranque anterior lo provoco el watchdog de 8 s en
vez de un reset normal, un tiron del USB o la apertura del puerto. Es un dato de
diagnostico, no de estado: el PC puede registrarlo para distinguir «la placa se
cayo y se levanto sola» de «alguien la desconecto». 

**El `hello` tiene que ser JSON valido en una sola linea.** Al construirse con
varios `Serial.print`, una coma de mas al final de un literal deja `,,` y el PC
rechaza el mensaje entero con `ProtocolError`, con lo que se pierden el modelo,
los contadores y el calentamiento. `tests/test_firmware.py` reconstruye la
cadena que imprime `emitHello()` y la pasa por `json.loads` para que ese fallo
no vuelva.

### 3.2 `ready`

Fin del calentamiento del PIR.

```json
{"t":"ready","warmup_ms":60000}
```

### 3.3 `hb` (latido)

Cada `hb_ms` (2 s por defecto). Durante el calentamiento llega cada 5 s con
`"warmup":1`.

```json
{"t":"hb","up":62000,"lvl":0,"raw":14,"buf":0,"lost":0,"calib":0}
```

| Campo | Significado |
|---|---|
| `up` | `millis()` del dispositivo |
| `lvl` | nivel actual de la salida del PIR (0/1) |
| `raw` | flancos crudos contados por la ISR desde el arranque |
| `buf` | eventos pendientes de acuse en el buffer circular |
| `lost` | eventos descartados por desbordamiento del buffer |
| `calib` | 1 si hay una sesion de calibracion activa |

### 3.4 `evt` (evento de movimiento)

```json
{"t":"evt","seq":2,"kind":"motion.start","z":1,"ms":64495}
{"t":"evt","seq":3,"kind":"motion.end","z":1,"ms":67195,"dur":2700}
{"t":"evt","seq":4,"kind":"bounce","z":1,"ms":70000}
```

| Campo | Significado |
|---|---|
| `seq` | numero de secuencia creciente; se reinicia al arrancar |
| `kind` | `motion.start`, `motion.end` o `bounce` |
| `ms` | hora del **flanco**, no de la confirmacion |
| `dur` | solo en `motion.end`: duracion del pulso |

**Importante:** `motion.end` no significa «la persona se ha ido». Significa que
el modulo termino su pulso de retencion. Con `Tx` al minimo son ~2,7 s fijos,
asi que dos personas separadas menos de ~2,9 s se funden en un solo pulso. La
deduplicacion con enfriamiento en el PC es obligatoria, no un extra.

### 3.5 `ack` (respuesta a comando)

```json
{"t":"ack","id":4,"ok":true,"op":"calib.start"}
```

### 3.6 `err`

```json
{"t":"err","code":"badop","msg":"unknown op"}
```

Codigos: `badcmd`, `badop`, `oversize`.

### 3.7 Calibracion

```json
{"t":"calib.edge","ms":1000,"lvl":1,"dt":2700}
{"t":"calib.window","ms":5000,"elapsed_ms":5000,"edges":2,"high_ms":5400,
 "duty":27.4,"min_gap":9000,"max_gap":12000,"bounces":0}
{"t":"calib.done","elapsed_ms":60000,"edges":3,"high_ms":8100,"duty":13.5,
 "min_gap":9000,"max_gap":22000,"gaps":2,"bounces":0}
```

`calib.edge` se emite por cada flanco confirmado mientras hay calibracion, con
`dt` = milisegundos desde el flanco anterior. `calib.window` llega cada
`window_ms` y `calib.done` al cerrar la sesion.

`bounces` cuenta flancos descartados por el antirrebote desde el inicio de la
ventana (o de la sesion, en `calib.done`). Es una diferencia entre dos contadores
y **nunca puede ser negativa**: el contador de ruido acumulado baja cuando el
antirrebote confirma un flanco, asi que la resta va protegida en el firmware.
Sin esa proteccion aparece `4294967295` en lugar de `0`; se detecto en una
prueba real y esta corregido.

---

## 4. Downlink (PC -> dispositivo)

```
cmd <id> ping
cmd <id> hello
cmd <id> set debounce_ms=50 hb_ms=2000 warmup_ms=60000 armed=0 buzzer=1
cmd <id> beep name=armed|disarmed|detect|alarm|error|test|tone|exit|entry|stop [ms=15000] [freq=2000] [force=1]
cmd <id> calib.start window_ms=5000 auto_end_ms=60000
cmd <id> calib.stop
cmd <id> replay
cmd <id> reset
ackevt <seq>
```

| Comando | Efecto |
|---|---|
| `ping` | solo responde `ack` |
| `hello` | reemite `hello`; sirve para resincronizar tras reconectar |
| `set` | cambia la configuracion en RAM. Los valores se acotan a rangos seguros |
| `beep` | reproduce un patron en el zumbador de D11. `ok=false` si el nombre no existe, si el zumbador esta apagado o si el PIR sigue calentando sin `force=1` |
| `calib.start` | inicia una sesion; se cierra sola al cumplirse `auto_end_ms` |
| `calib.stop` | cierra la sesion y emite `calib.done` |
| `replay` | reemite los eventos sin acusar |
| `reset` | pone a cero los contadores de flancos y ruido |
| `ackevt <seq>` | el PC confirma todos los eventos hasta `seq` |

### 4.1 Estado remoto del zumbador

`set` admite tres claves que no son medidas del sensor:

| Clave | Valores | Efecto |
|---|---|---|
| `armed` | `0` / `1` | Habilita el pitido local de deteccion. Con `0` la placa no pita al detectar, aunque el PC siga avisando |
| `buzzer` | `0` / `1` | Interruptor general del zumbador. Con `0` se corta el patron en curso |
| `siren_ms` | 1000..600000 | Tope del patron `alarm`, que es ciclico. Evita que la sirena quede sonando si el PC desaparece. Por defecto 120000 (2 min) |

### 4.2 `beep`

```
cmd <id> beep name=armed
cmd <id> beep name=alarm ms=120000
cmd <id> beep name=tone freq=2700 ms=1200 force=1
cmd <id> beep name=exit ms=45000 force=1
cmd <id> beep name=stop
```

| Nombre | Patron |
|---|---|
| `armed` | dos pitidos agudos (1800 Hz) |
| `disarmed` | un tono grave largo (650 Hz) |
| `detect` | tres pitidos rapidos (1500 Hz) |
| `alarm` | sirena alterna ciclica, acotada por `ms` (por defecto `siren_ms`) |
| `error` | un tono muy grave (380 Hz) |
| `test` | tres tonos ascendentes |
| `tone` | tono continuo a `freq` Hz (100..8000, por defecto 2000) durante `ms` (por defecto 1000) |
| `exit` | cuenta atras de salida: pitidos de 1000 ms que se aceleran hasta 250 ms, a 1800 Hz |
| `entry` | cuenta atras de entrada: pitidos de 700 ms hasta 150 ms, a 2200 Hz |
| `stop` | corta en el acto el patron, la sirena o la cuenta atras en curso. Siempre responde `ok=true`, aunque `buzzer=0` |

| Clave opcional | Efecto |
|---|---|
| `ms` | limite en ms. En `alarm` acota la sirena, en `tone` es la duracion y en `exit`/`entry` es la duracion total de la cuenta atras (por defecto 45000 y 30000) |
| `freq` | solo para `tone` (y para cambiar el tono de `exit`/`entry`): frecuencia en Hz, acotada a 100..8000 |
| `force` | `1` salta el silencio del calentamiento. Lo usan las pruebas manuales: sin el, probar el zumbador recien conectada la placa devolvia `ok=false` |

`exit` y `entry` generan los pitidos **en la placa**, en `beeperTick`, sin
`delay()`: la ventana manda una sola orden con la duracion y la cadencia se
acelera alli. Si el PC desaparece a mitad, la cuenta atras termina sola y deja
de sonar; no hay pitidos mandados uno a uno que puedan quedar a medias.

El zumbador calla durante el calentamiento del PIR y mientras `buzzer=0`; en
ambos casos el `ack` llega con `ok=false`, que es la forma de que la ventana
distinga «la placa no obedece» de «el cableado esta mal». Si el nombre no
existe, el dispositivo responde `err` con `badop` y `ack` con `ok=false`, igual
que cualquier otra operacion desconocida.

`id` es un entero creciente que pone el PC; el `ack` lo devuelve para poder
emparejar peticion y respuesta.

---

## 5. Fiabilidad

- **Secuencia.** El dispositivo numera los eventos. El PC los acusa con
  `ackevt` y el dispositivo los libera del buffer circular (16 posiciones).
- **Desbordamiento.** Si el buffer se llena, se descarta el mas antiguo y se
  incrementa `lost`, que viaja en cada latido. Nunca se pierde en silencio.
- **Reconexion.** Al abrir el puerto, el DTR reinicia la placa. El PC lo detecta
  porque `boot` cambia, y resincroniza el contador de secuencia.
- **Lineas invalidas.** Se descartan y se cuentan en `stats.decode_errors`.
  Nunca detienen el bucle de lectura.
- **Latido.** Si no llega ninguna linea durante `hb_timeout` (8 s por defecto),
  el enlace se marca `OFFLINE`.

---

## 6. Calentamiento y reset por DTR

Abrir el puerto serie pone DTR en bajo y **reinicia el microcontrolador**. Eso
tiene dos consecuencias:

1. El firmware arranca de cero: `boot` cambia, `seq` vuelve a 1 y empieza un
   calentamiento de 60 s.
2. El HC-SR501 **no se apaga**: su alimentacion viene del rail de 5 V del Mega
   y el sensor conserva su estabilizacion termica.

Por eso, tras el primer minuto de encendido real, un reinicio por DTR no obliga
a esperar otros 60 s: se puede enviar `cmd <id> set warmup_ms=0` y el firmware
cierra el calentamiento de inmediato. `tools/alarmctl.py` lo hace con
`--skip-warmup`.

---

## 7. Modo de ajuste del sensor

Los potenciometros del HC-SR501 son fisicos: ningun software puede girarlos.
Lo que hace el modo de calibracion es medir su efecto y traducirlo a
instrucciones.

| Potenciometro | Que controla | Como se manifiesta | Como se mide |
|---|---|---|---|
| `Sx` | sensibilidad y alcance | falsos positivos o ausencia de deteccion | fase de reposo |
| `Tx` | tiempo que la salida sigue alta | duracion de `motion.end` y fusion de eventos | fase de movimiento |

El asistente (`alarmctl.py calibrate`) ejecuta dos fases:

1. **Reposo** (30 s por defecto). Nadie se mueve. Se cuentan detecciones.
   - 0 detecciones: `Sx` correcta para esa ubicacion.
   - 1-2: aviso. Mascota, aire acondicionado o `Sx` algo alto.
   - 3 o mas: fallo. Girar `Sx` al minimo y revisar la ubicacion.
2. **Movimiento** (45 s). El usuario cruza la estancia 3-4 veces.
   - 0 pulsos: no detecta. Revisar cableado, luego subir `Sx`, luego acercarse.
   - Pulsos constantes de ~2,7 s: `Tx` al minimo (es el minimo del modulo).
   - Pulsos de mas de 15 s: `Tx` demasiado alto, los eventos se fusionan.
   - Salida alta mas del 85 % del tiempo: saturacion, `Sx` demasiado alto.

El diagnostico se calcula en `src/alarm/device/calibrator.py`, funcion
`evaluate()`, y esta cubierto por pruebas con umbrales explicitos.
