# Validación de hardware: campaña de la versión 0.1.5

Registro de la campaña de validación **sobre la placa física**. El software de la
versión 0.1.5 está cerrado y la CI está en verde; el firmware ya compila en el
banco (HW-01), y lo que falta es demostrar que se flashea y se comporta como
describe la documentación cuando hay un Mega 2560, un HC-SR501 y un zumbador de
verdad conectados.

| Campo | Valor |
|---|---|
| Documento | `doc/05-VALIDACION-HARDWARE.md` |
| Versión | 1.1 |
| Fecha | 2026-11-13 |
| Estado | **En ejecución** — HW-01, HW-02 y HW-03A `PASS`; HW-06 parcial; el resto pendiente |
| Ámbito | Arduino Mega 2560 + HC-SR501 + zumbador pasivo + aplicación de escritorio |
| Software de referencia | `a61198a` — versión **0.1.5**, firmware **1.7.0** |
| Audiencia | Quien monta, flashea y valida el equipo; mantenimiento |

---

## 1. Alcance

Este documento **no reabre el software**. Su punto de partida es el dictamen ya
emitido:

- `main` en `a61198a`, con la versión 0.1.5 íntegra desde `f3b1a9c`.
- PR #2 (versión 0.1.5) fusionado en `0cf8e28`; PR #3 (aserción del test de la
  sirena) fusionado en `a61198a`. Ese PR corrigió el único fallo de CI que tuvo
  `main`: la ejecución sobre `0cf8e28` quedó en rojo por
  `tests/test_ui.py::test_un_disparo_guardado_se_recupera_y_reanuda_la_sirena`,
  y la punta `a61198a` está en verde.
- 616 pruebas en verde en Python 3.11 y 3.13, `ruff`, `ruff format --check` y
  `mypy` limpios, CI del repositorio en verde. La mitad de 3.11 se acredita con
  la matriz del CI; en el banco solo está instalado 3.13.
- Los commits posteriores a `a61198a` (`a17c084` y `78b98c2`) solo tocan este
  documento y el `README`: `origin/main` sigue en `a61198a` y el software bajo
  prueba no cambia.

Lo que aquí se somete a prueba es la capa física y su interacción con ese
software ya cerrado. Un `FAIL` de esta campaña **no** invalida el PASS de
software: abre una incidencia de hardware o de firmware que se corrige en una
fase posterior, con su propio PR y su propia revisión.

## 2. Qué certifica esta campaña y qué no

| Capa | Qué se demuestra | Cómo | Estado |
|---|---|---|---|
| Software en simulación | Lógica de la FSM, protocolo, persistencia, avisos | `pytest`, `tools/alarmctl.py --sim info`, Qt *offscreen* | **Certificado** (616 pruebas, §1) |
| Compilación del firmware | El sketch compila para `arduino:avr:mega` | HW-01 | **PASS** |
| Enlace serie real | `hello`, `hb`, `evt`, `ack` sobre USB | HW-02, HW-03A | **PASS** (HW-02 y HW-03A); `evt` pendiente |
| Errores de comando | `err` con `badop` y `oversize` | HW-03B | **BLOQUEADO** (sin remitente de líneas crudas) |
| Buffer y ACK | 16 eventos, reenvío, `lost`, deduplicación | HW-04 | Pendiente |
| Durabilidad | SQLite `FULL`, spool, recuperación tras corte | HW-05 | Pendiente |
| Sirena y FSM reales | Plazos, corte, rearme, retoma tras reinicio | HW-06 | Pendiente |
| Watchdog e identidad | `wdt_rst`, `boot_id` en arranques reales | HW-07 | Pendiente (observacional; no se fuerza) |
| Operación continua | 72 h, desenchufe de 1 h | HW-08 (opcional) | Pendiente |

Dos reglas de lectura:

1. **Una prueba simulada no cuenta como prueba de placa.** `--sim` y `pytest`
   demuestran que el código hace lo que dice; no demuestran que el cable, el
   PIR o el piezo funcionen.
2. **Un escenario sin fila en la tabla resumen (§7) no está ejecutado.** No se
   hereda el resultado de una campaña anterior ni se copia de la documentación.

## 3. Hoja de banco

Se rellena una vez por sesión de pruebas y se repite si cambia cualquier
elemento. Sin estos datos, una fila de resultado no es interpretable.

### Sesión 1 — 2026-11-13 23:24, solo compilación (HW-01)

| Campo | Valor |
|---|---|
| Fecha y hora de inicio | 2026-11-13 23:24 |
| Operador | jvelasca |
| Alcance de la sesión | Solo compilación (HW-01): no se flashea y no se abre el puerto serie |
| Placa (modelo y origen) | Mega 2560 con adaptador CH340, detectada en `COM3` |
| Puerto | `COM3` (detectado; no se abrió en esta sesión) |
| Alimentación | — (no aplica: no se alimentó la placa desde el banco) |
| Cable USB | — (no se usó; se verifica en HW-02) |
| `arduino-cli` (ruta y versión) | 1.5.1 — `%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe` |
| Core `arduino:avr` | 1.8.8 |
| Windows | Windows 11 Pro 10.0.26200 |
| Firmware anunciado en `hello` | — (no se abrió el puerto) |
| Commit bajo prueba | `a61198a` (firmware); el árbol incluye `a17c084` y `78b98c2`, que solo tocan este documento |
| Configuración cargada | — (no aplica: HW-01 no carga `config/alarm.toml`) |
| Jumper del HC-SR501 y ajuste | — (no aplica: el sensor no se conecta en HW-01; se anota en HW-02) |

Al empezar una sesión nueva se abre otra hoja: la placa, el cable, la
alimentación y el ajuste del PIR solo se pueden declarar cuando se prueban.

Comandos de la sesión 1, desde la raíz del proyecto:

```powershell
# Herramientas de compilación: la misma ruta que usa flash.ps1
$cli = Join-Path $env:LOCALAPPDATA 'Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe'
& $cli version
& $cli core list
```

Los comandos de identificación del dispositivo (`alarmctl.py ports` y
`alarmctl.py info`) no forman parte de una sesión de solo compilación: `info`
abre el puerto y reinicia la placa por DTR. Se reservan para la sesión de HW-02.

`scripts/flash.ps1` localiza `arduino-cli` en `%LOCALAPPDATA%` o en
`%ProgramFiles%`, y solo recurre al `PATH` si no está en ninguno de los dos
sitios. Que `arduino-cli` no esté en el `PATH` no impide compilar.

### Sesión 2 — 2026-11-14 00:10, placa conectada y flasheada (HW-02)

| Campo | Valor |
|---|---|
| Fecha y hora de inicio | 2026-11-14 00:10 |
| Operador | jvelasca |
| Alcance de la sesión | Flasheo, `hello`, `boot_id` y omisión del calentamiento (HW-02) |
| Placa (modelo y origen) | Mega 2560 con adaptador CH340 (`VID_1A86`, `PID_7523`), `COM3` |
| Puerto | `COM3`, detectado por VID/PID por `flash.ps1` y por `serial_watch.ps1` |
| Alimentación | Solo USB (5 V). No se conectó `VIN`; `VIN` se prueba en HW-04B |
| Cable USB | Datos: la subida por `avrdude` completó, un cable de solo carga no lo permitiría |
| `arduino-cli` (ruta y versión) | 1.5.1 — `%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe` |
| Core `arduino:avr` | 1.8.8 |
| Windows | Windows 11 Pro 10.0.26200 |
| Firmware anunciado en `hello` | `1.7.0` |
| Commit bajo prueba | `a61198a` (firmware y software); el árbol incluye commits que solo tocan este documento |
| Configuración cargada | — (sin ventana abierta: la sesión se hizo con `alarmctl` y `serial_watch`) |
| Jumper del HC-SR501 y ajuste | — (no se ajustó ni se comprobó en esta sesión; se anota al probar el sensor) |

En esta sesión se subió el sketch con `flash.ps1` (16 048 B de flash, 1 022 B de
RAM) y los `hello` capturados muestran `boot_id` 3 y 4: el contador sube en cada
reinicio por DTR.

## 4. Protocolo de evidencia

- **Resultados admitidos:** `PASS`, `FAIL`, `BLOQUEADO`. Un escenario no
  ejecutado se queda como *pendiente*; no se marca `PASS` por analogía.
- **Una fila por ejecución.** Si un escenario se repite, se añade otra fila con
  su fecha; no se sobrescribe la anterior.
- **Logs en bruto** en `data\validacion\`, con nombre
  `HW-0X-AAAAMMDD-HHMM-<sufijo>.log`; si el escenario tiene subcasos, la letra va
  detrás del número (`HW-04C-20261114-1012-buffer.log`). `data\` está ignorado
  por git, así que esos ficheros no se publican: contienen horas de presencia y
  datos de la vivienda. En el documento se copia solo el extracto mínimo y la
  ruta. **El buzón durable no está en `data\`**, sino en `.\spool` (ver §6
  HW-05): `data\` no lo tapa y `.\spool` sí puede acabar versionado.
- **Evidencia mínima por fila:** un extracto de log con marca de tiempo, una
  captura de la fila del historial, o el código de salida de un comando.
- **No se corrige el resultado editando el documento.** Un `FAIL` se registra
  como tal y abre incidencia (§8). El documento no es el sitio donde se
  arreglan las cosas.
- **Nada de cambios de producción durante la campaña.** Si un escenario revela
  un fallo, se documenta; la corrección va en un PR aparte, con su propia
  revisión y su propia validación.

## 5. Orden de ejecución

1. **HW-01** primero: si no compila, no hay nada que probar. **PASS.**
2. **HW-02** y **HW-03A**: enlace y órdenes, que es la base de todo lo demás.
   Los dos `PASS`; HW-03B y el rechazo con `buzzer=0` constan `BLOQUEADO` y no
   interrumpen el orden.
3. **HW-06**: sirena y FSM, con la ventana abierta y plazos acortados.
4. **HW-04** y **HW-05**: desconexiones y cortes, que ensucian el estado y se
   hacen mejor con el resto ya validado.
5. **HW-07** en paralelo, aprovechando los reinicios de los anteriores.
6. **HW-08** al final y en una sesión aparte, porque dura tres días.

---

## 6. Escenarios

### HW-01 — Compilación reproducible del firmware

**Objetivo.** Demostrar que `firmware/mega-pir/mega-pir.ino` compila para
`arduino:avr:mega` con el entorno instalado en el banco, y registrar el consumo
real de flash y RAM.

**Precondiciones.** Arduino IDE 2.x o `arduino-cli` instalado, core
`arduino:avr` descargado, repositorio en `a61198a`.

**Procedimiento.**

1. Compilar sin subir:

   ```powershell
   .\scripts\flash.ps1 -CompileOnly
   ```

2. Anotar la ruta de `arduino-cli` que imprime el script, su versión y la del
   core `arduino:avr`.
3. Anotar los bytes de flash y de RAM global que reporta el compilador.
4. Comprobar que la versión del sketch sigue siendo la esperada:

   ```powershell
   Select-String -Path firmware\mega-pir\mega-pir.ino -Pattern 'FW_VERSION'
   ```

**Evidencia.** Salida completa de `flash.ps1 -CompileOnly`, con el bloque de
tamaños; y la línea de `FW_VERSION`.

**Criterio de aceptación.**

- Código de salida 0, sin errores de compilación.
- `FW_VERSION` vale `1.7.0`.
- Sin avisos de desbordamiento de flash ni de RAM.
- Los tamaños son coherentes con la referencia de
  [`00-PREMISAS.md`](00-PREMISAS.md) §4.4 (15 404 bytes de flash, 1 018 de RAM
  con el core del momento). Una diferencia pequeña por versión de core o de IDE
  se anota, no se considera `FAIL`.

**Resultado.**

| Resultado | Fecha | Operador | Evidencia |
|---|---|---|---|
| **PASS** | 2026-11-13 23:24 | jvelasca | `data\validacion\HW-01-20261113-2324-compile.log` — `exit=0`, 16 048 B de flash (6 %), 1 022 B de RAM (12 %), `FW_VERSION` 1.7.0 |

`flash.ps1 -CompileOnly` salió con código 0 y sin avisos; una segunda
compilación con `arduino-cli compile --clean` para el mismo sketch dio
exactamente los mismos tamaños (16 048 B / 1 022 B), así que la medida no
procede de una caché de compilación.

**Nota sobre la diferencia con la referencia.** Los 15 404 B de flash y 1 018 B
de RAM de [`00-PREMISAS.md`](00-PREMISAS.md) §4.4 se midieron con el firmware
**1.6.0** (commit `a244f85`, entrada 0.1.2 del `CHANGELOG`). Esta compilación es
del firmware **1.7.0**, que añade `boot_id` en EEPROM con dos ranuras
ping-pong, magic y CRC16 (unas 100 líneas de sketch), `FW_VERSION` y el campo
`boot_id` del `hello`; el reacuse de reenvíos es un cambio del PC en la 0.1.5
(H-02 del `CHANGELOG`), no del firmware. Los 644 B más de flash y 4 B más de RAM
son la evolución del sketch entre esas dos versiones, no un cambio de entorno:
el core y el `arduino-cli` usados quedan anotados en la hoja de banco (§3). No
se considera `FAIL` ni se corrige la referencia dentro de esta campaña.

### HW-02 — Flasheo, sesión y saludo

**Objetivo.** Subir el firmware a la placa real y comprobar que la sesión se
identifica: `proto`, modelo, versión de firmware, `boot_id`, calentamiento y
acuse del `set warmup_ms=0`.

**Precondiciones.** HW-01 en `PASS`. Placa conectada por un cable de datos.

**Procedimiento.**

1. Subir el sketch; si hay varias placas, indicar el puerto:

   ```powershell
   .\scripts\flash.ps1
   .\scripts\flash.ps1 -Port COM3
   ```

2. Identificar el dispositivo:

   ```powershell
   .\.venv\Scripts\python.exe tools\alarmctl.py info
   ```

   `info` abre el puerto, lo que reinicia la placa por DTR y arranca un
   calentamiento nuevo. Resume el `hello` (modelo, protocolo, firmware,
   capacidades, zonas y `cfg`) pero **no imprime `boot_id`**: eso se lee en la
   captura en bruto del paso siguiente.

3. Capturar el arranque en bruto durante unos segundos:

   ```powershell
   .\tools\serial_watch.ps1 -Seconds 8 -LogFile data\validacion\HW-02-AAAAMMDD-HHMM-hello.log
   ```

4. Cerrar el monitor, abrir el puerto otra vez y repetir. Comparar los dos
   `hello`.
5. Con la placa ya caliente, omitir el calentamiento desde la consola:

   ```powershell
   .\.venv\Scripts\python.exe tools\alarmctl.py monitor --seconds 20 --skip-warmup
   ```

**Evidencia.** Los dos `hello` completos, con `boot_id` y `cfg`; y el log del
paso 5 mostrando que no se esperan 60 s.

**Criterio de aceptación.**

- `hello` es JSON válido en **una sola línea**, con `proto: 1`, `dev: "mega-pir"`
  y `fw: "1.7.0"`. `serial_watch.ps1` antepone a cada línea `    1234 ms | `,
  así que para validarlo hay que quitar ese prefijo.
- `zones` anuncia la zona 1 en el pin 2 y `cfg.buzzer_pin` vale 11.
- `boot_id` **aumenta** entre los dos arranques del paso 4 (es lo que distingue
  una sesión de otra; `boot` vale ~0 en ambos y no sirve).
- `cfg.warmup_ms` es 60000 y el calentamiento se cierra al pedir
  `warmup_ms=0`, sin esperar el minuto (no hace falta reiniciar la placa para
  saltarlo).
- `cfg.wdt_rst` vale 0 en un arranque por DTR.

**Resultado.**

| Resultado | Fecha | Operador | Evidencia |
|---|---|---|---|
| **PASS** | 2026-11-14 00:14 | jvelasca | `data\validacion\HW-02-20261114-0014-hello-a.log`, `-hello-b.log` y `HW-02-20261114-0015-skipwarmup.log` |

Extracto de los dos `hello` (el prefijo `<ms> ms |` de `serial_watch` recortado):

```
{"t":"hello","proto":1,"dev":"mega-pir","fw":"1.7.0","boot":6,"boot_id":3,"zones":[{"id":1,"pin":2,"name":"pir"}],
 "caps":["hello","set","replay","calibrate","beep"],
 "cfg":{"warmup_ms":60000,"debounce_ms":50,"hb_ms":2000,"calib_window_ms":5000,"buzzer_pin":11,"buzzer":1,"armed":0,"wdt_rst":0}}
{"t":"hello","proto":1,"dev":"mega-pir","fw":"1.7.0","boot":6,"boot_id":4, ... resto idéntico ...}
```

`boot` vale 6 ms en los dos arranques —no distingue sesiones— y `boot_id` sube
de 3 a 4, que es exactamente lo que documenta
[`02-PROTOCOLO.md`](02-PROTOCOLO.md). `alarmctl.py info` sobre la misma sesión
confirma `dispositivo mega-pir (protocolo v1)`, `firmware 1.7.0`, `zona 1: pir
en D2`, `buzzer_pin 11`, `warmup_ms 60000` y `wdt_rst 0`, y no imprime `boot_id`.

Paso 5: `monitor --seconds 20 --skip-warmup` abrió el puerto, reinició la placa
por DTR y cerró el calentamiento en **0 ms** («PIR listo tras 0 ms de
calentamiento»), sin esperar los 60 s. La sesión dejó 9 latidos con `buf: 0` y
`lost: 0`, y ningún evento.

La subida con `flash.ps1` detectó `COM3` por VID/PID (CH340), compiló con los
mismos 16 048 B de flash y 1 022 B de RAM de HW-01 y terminó con «Subida
completada en COM3».

### HW-03 — Órdenes del protocolo y acuses

**Objetivo.** Comprobar sobre la placa que cada orden se acusa con su `id`, que
los nueve patrones del zumbador suenan y que los caminos de rechazo se
distinguen de «la placa no obedece».

**Precondiciones.** HW-02 en `PASS`. Placa caliente o `--skip-warmup`. Volumen
del piezo audible, sin molestar a nadie durante la prueba.

**Procedimiento (HW-03A — patrones).**

1. Escuchar los patrones de uno en uno, con la placa caliente:

   ```powershell
   .\.venv\Scripts\python.exe tools\alarmctl.py beep --name armed --skip-warmup
   .\.venv\Scripts\python.exe tools\alarmctl.py beep --name disarmed --skip-warmup
   .\.venv\Scripts\python.exe tools\alarmctl.py beep --name detect --skip-warmup
   .\.venv\Scripts\python.exe tools\alarmctl.py beep --name error --skip-warmup
   .\.venv\Scripts\python.exe tools\alarmctl.py beep --name test --skip-warmup
   .\.venv\Scripts\python.exe tools\alarmctl.py beep --name tone --freq 2700 --ms 1500 --skip-warmup
   .\.venv\Scripts\python.exe tools\alarmctl.py beep --name exit --ms 8000 --skip-warmup
   .\.venv\Scripts\python.exe tools\alarmctl.py beep --name entry --ms 8000 --skip-warmup
   .\.venv\Scripts\python.exe tools\alarmctl.py beep --name alarm --ms 4000 --skip-warmup
   ```

2. Comprobar que `stop` se acusa siempre con `ok=true`, incluso sin nada
   sonando:

   ```powershell
   .\.venv\Scripts\python.exe tools\alarmctl.py beep --name stop --skip-warmup
   ```

   Cortar una sirena **en curso** con `stop` no se puede hacer así: cada
   invocación de `alarmctl` abre el puerto, y abrirlo reinicia la placa por DTR.
   Ese corte se prueba desde la ventana, que mantiene el puerto abierto (HW-06),
   o con el botón de silenciar.

3. Rechazo de la placa (`ok=false`): **BLOQUEADO** en esta campaña, por el mismo
   motivo que HW-03B. El firmware solo rechaza el patrón si `g_buzzerOn` está a
   0 (`mega-pir.ino`, `beeperByName`), y ese interruptor vive en RAM con valor
   inicial 1: cualquier invocación de `alarmctl beep` abre el puerto, la placa se
   reinicia por DTR y vuelve a tener el zumbador encendido. El mensaje «el
   dispositivo rechazo el patron» y la salida con error existen
   (`tools/alarmctl.py`, `cmd_beep`), pero no hay forma de llegar a ellos con las
   herramientas actuales. Desactivar el zumbador en **Alarma → Zumbador…**
   (`Ctrl+Alt+B`) tampoco sirve: al cerrar la ventana se pierde el `set
   buzzer=0`, y el botón **Probar zumbador** ni siquiera envía la orden cuando la
   casilla está desmarcada —avisa con «El zumbador está desactivado en la
   configuración.» o, dentro del diálogo, «Activa «Pitidos locales en la placa»
   para poder probar.»—. Queda pendiente de una herramienta que mantenga la
   sesión abierta, como HW-03B.

**Procedimiento (HW-03B — errores de comando).** Enviar una orden con una
operación inexistente y una línea de más de 96 bytes debe producir `err` con
`badop` y `oversize` respectivamente. **No hay hoy ningún remitente de líneas
crudas**: `alarmctl` valida las opciones con `argparse` y
`tools/serial_watch.ps1` solo lee. Este subcaso queda `BLOQUEADO` hasta que
exista una herramienta de envío crudo (fase posterior) o se acuerde un
procedimiento de banco equivalente.

**Evidencia.** Un log por patrón con su `ack` (`id`, `ok`, `op`) y la duración
medida de la sirena acotada por `ms`. El rechazo con `buzzer=0` no genera
evidencia en esta campaña: está `BLOQUEADO`.

**Criterio de aceptación.**

- Cada orden recibe su `ack` con el mismo `id` que puso el PC.
- Los nueve patrones se distinguen entre sí: dos pitidos agudos (`armed`), tono
  grave largo (`disarmed`), tres rápidos (`detect`), tono muy grave (`error`),
  tres ascendentes (`test`), continuo a 2700 Hz (`tone`), cadencias aceleradas
  de salida y entrada (`exit`, `entry`) y alternancia cíclica (`alarm`).
- `alarm --ms 4000` calla solo a los ~4 s, no a los 120 s por defecto.
- `stop` responde `ok=true` siempre, sin depender de que haya un patrón en
  curso.
- Rechazo con `buzzer=0` en la placa: `BLOQUEADO` en esta campaña, con el motivo
  escrito. No se admite deducirlo de otra prueba.
- `err` con `badop`/`oversize`: `BLOQUEADO` en esta campaña, con el motivo
  escrito.

**Resultado.**

| Resultado | Fecha | Operador | Evidencia |
|---|---|---|---|
| **PASS** (HW-03A, patrones) | 2026-11-14 00:19 | jvelasca | `data\validacion\HW-03A-20261114-0019-patrones.log` — diez órdenes, `exit=0` en todas |
| **BLOQUEADO** (HW-03B) | 2026-11-13 | jvelasca | — sin remitente de líneas crudas: `argparse` en `alarmctl` y `serial_watch.ps1` de solo lectura |
| **BLOQUEADO** (HW-03A, rechazo con `buzzer=0`) | 2026-11-13 | jvelasca | — el interruptor del zumbador vive en RAM y el DTR lo repone al abrir el puerto |

Los diez comandos (`armed`, `disarmed`, `detect`, `error`, `test`, `tone
--freq 2700 --ms 1500`, `exit --ms 8000`, `entry --ms 8000`, `alarm --ms 4000`
y `stop`) devolvieron `OK` y código 0. `alarmctl` no imprime el `id` ni el `op`
del `ack`: espera el acuse del `id` que envió y, si no llega, falla con «sin
respuesta al comando beep», así que el `exit=0` es la prueba de que el acuse
casó. `stop` respondió `ok` sin nada sonando, como exige el criterio.

La parte que no deja rastro en un log la confirmó el operador: los nueve
patrones se distinguen entre sí y la sirena de `alarm --ms 4000` calló a los
~4 s, no a los 120 s por defecto. Ese juicio se anota aquí y no se deduce del
código.

### HW-04 — Buffer de 16, reenvío y desconexiones

**Objetivo.** Verificar los dos modos de fallo del enlace: pérdida de
alimentación (la placa se reinicia) y pérdida del enlace con la placa todavía
alimentada (el ACK se pierde y el evento debe reenviarse).

**Precondiciones.** HW-02 y **HW-03A** en `PASS` (HW-03B está `BLOQUEADO` y no
condiciona este escenario). Para HW-04B, una fuente externa de
7-12 V en `VIN`/`GND`, de modo que quitar el USB no apague la placa.

**Procedimiento (HW-04A — tirón del USB, con la placa alimentada solo por USB).**

1. Con la ventana conectada y la alarma armada, provocar movimiento y, **acto
   seguido**, desenchufar el cable USB.
2. Volver a enchufarlo y esperar a que la ventana reconecte sola.
3. Anotar el `hello` nuevo (`boot_id` distinto y `warmup_ms`) y el primer `evt`,
   que trae `seq=1`: el `hello` no lleva `seq`.
4. Comprobar el historial: un `start` por detección, sin filas duplicadas y sin
   avisos repetidos.

**Procedimiento (HW-04B — pérdida de enlace con la placa viva).** Este subcaso
solo es concluyente si al reabrir el puerto la placa **no** se reinicia.

1. Alimentar la placa por `VIN` con 7-12 V y quitar el USB. La placa sigue
   ejecutándose, pero el chip USB-serie se apaga con el cable, así que el puerto
   desaparece del sistema.
2. Provocar entre 3 y 8 pulsos del PIR (cada pulso genera `motion.start` y
   `motion.end`), sin PC al otro lado.
3. Reenchufar el USB y mirar el `hello`: si `boot_id` cambia, el adaptador pulsa
   DTR al abrir el puerto y este subcaso es en realidad un reinicio (HW-04A).
   Con un CH340 o ATmega16U2 habitual eso es lo esperable. Si `boot_id` **no**
   cambia, la sesión sigue viva y aquí se puede medir el reenvío.
4. Si la sesión sigue viva: la ventana pide `cmd replay` al recibir el `hello`,
   llegan los `evt` pendientes y se reacusan; `buf` baja a 0 y el historial no
   gana una segunda fila del mismo evento. El contador `duplicates` del gestor
   existe, pero hoy no se muestra en ninguna parte: úsese el historial como
   evidencia, no ese contador.
5. Repetir quitando el USB durante la sirena: al reconectar no debe cortarse ni
   reiniciarse la sirena por un ACK atrasado de otra sesión.

El código contempla las dos ramas (ver [`02-PROTOCOLO.md`](02-PROTOCOLO.md) §5:
«si el adaptador no pulsa DTR y la placa sigue en la misma sesión, el acuse se
conserva»). Si el banco no tiene un adaptador que no pulse DTR, **04B se marca
`BLOQUEADO`** con ese motivo, y la única prueba física posible es 04A.

**Procedimiento (HW-04C — desbordamiento del buffer).**

1. Sin la aplicación (para que nadie acuse), capturar el puerto:

   ```powershell
   .\tools\serial_watch.ps1 -Seconds 240 -LogFile data\validacion\HW-04C-AAAAMMDD-HHMM-buffer.log
   ```

2. Esperar el `ready` y cruzar la estancia de forma continua durante unos 90 s,
   sin parar entre pulsos.
3. Buscar en el log los latidos con `buf` creciente y el punto en que aparece
   `lost > 0`.

**Evidencia.** Los `hello` de antes y después; el tramo de log con los `evt`
repetidos tras el `hello` —el `replay` y los `ackevt` van del PC a la placa y no
aparecen en una captura de solo lectura—; y la secuencia de `hb` con
`buf`/`lost`.

**Criterio de aceptación.**

- **04A:** `boot_id` distinto y el primer `evt` con `seq=1`; la ventana reconecta sin
  intervención; ninguna detección se procesa dos veces; ninguna sirena arranca
  sola tras el reinicio si el estado persistido está desarmado.
- **04B:** si el adaptador no pulsa DTR, `boot_id` no cambia, los eventos
  pendientes se reenvían y se reacusan, `buf` vuelve a 0 y un ACK de la sesión
  anterior no se aplica a la nueva. Si el adaptador pulsa DTR, 04B queda
  `BLOQUEADO` y no se cuenta como `PASS` por lo que haga 04A.
- **04C:** el buffer se llena a los 16 eventos y `lost` empieza a subir. La
  pérdida **no es silenciosa mientras la sesión vive**: se ve en el latido.
- **Límite que hay que registrar, no ocultar:** tras un corte de alimentación
  (`04A`), el contador `lost` vuelve a 0 con el arranque y los eventos que
  quedaban en RAM se pierden sin posibilidad de contarlos. Eso es el
  comportamiento documentado en [`02-PROTOCOLO.md`](02-PROTOCOLO.md) §5 y en
  [`00-PREMISAS.md`](00-PREMISAS.md) §4.5; no es un `FAIL`, pero el escenario
  `04A` no puede usarse para demostrar que no se pierde nada.

**Resultado.**

| Resultado | Fecha | Operador | Evidencia |
|---|---|---|---|
| pendiente (04A) | | | |
| pendiente (04B) | | | |
| pendiente (04C) | | | |

### HW-05 — SQLite y spool ante un corte de alimentación del PC

**Objetivo.** Comprobar que el acuse durable de 0.1.5 resiste un corte real del
PC: el evento acusado ya está en disco (`synchronous=FULL`), el spool se
reimporta solo y no hay efectos duplicados.

**Precondiciones.** HW-04A y HW-04C en `PASS` (HW-04B no condiciona: nace con
alta probabilidad de `BLOQUEADO` con el adaptador CH340). Historial con datos
reconocibles y, si se
quiere conservarlo, exportado antes de empezar (**Archivo → Exportar registro…**,
`Ctrl+E`).

**Procedimiento.**

1. Armar con retardo de entrada corto (0 o 10 s, `Ctrl+Alt+R`) para disparar
   rápido.
2. Provocar detección y, en tres repeticiones, cortar la alimentación del PC en
   momentos distintos:
   - (a) durante la ráfaga de eventos del PIR;
   - (b) durante la sirena, con la alarma `DISPARADA`;
   - (c) con la aplicación inactiva y el estado ya guardado.
3. Tras cada reinicio, antes de abrir la aplicación, mirar el buzón:

   ```powershell
   Get-ChildItem .\spool -ErrorAction SilentlyContinue
   ```

4. Abrir la aplicación y dejar que drene el spool.
5. Comprobar la base y el historial:

   ```powershell
   .\.venv\Scripts\python.exe -c "import sqlite3; c=sqlite3.connect('data/alarm.db'); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
   .\.venv\Scripts\python.exe tools\alarmctl.py log --since -2h --tag triggered
   .\.venv\Scripts\python.exe tools\alarmctl.py log --since -2h --tag start
   ```

   El filtro es `--tag`, no `--kind`: `--kind` busca texto en la columna del
   tipo de evento («Movimiento», «Fin de pulso»), que es prosa y no un
   identificador.

6. Comparar con los avisos realmente enviados (registro de la ventana y bandeja
   de correo) para descartar duplicados.

**Procedimiento (HW-05D — fallo simultáneo de SQLite y del buzón).** Es la
prueba directa de la garantía de 0.1.5: sin escritura durable no hay `ackevt`.
Se hace con la ventana **cerrada**, al final de la sesión, y se restaura en el
acto.

**Dónde está el buzón.** No está en `data\`: `MainWindow` lo sitúa en
`STORAGE_ROOT / "spool"`, y `STORAGE_ROOT` es la raíz de datos —la raíz del
proyecto cuando se arranca con `scripts/run.ps1` o desde el depurador, la
carpeta del `.exe` si está empaquetada—. Es decir, **`.\spool`**. Solo aparece
cuando hace falta escribir el primer evento, y no lo ignora git. `00-PREMISAS.md`
§4.5 y `02-PROTOCOLO.md` §5 lo llaman `data/spool/`: la ruta del código es
`.\spool` y este procedimiento sigue el código; la discrepancia de esos dos
documentos queda anotada para corregirla aparte, porque no se toca producción
durante la campaña.

1. Con la ventana cerrada, apartar el historial y el buzón y preparar el
   fallo:

   ```powershell
   New-Item -ItemType Directory data\guardia | Out-Null
   if (Test-Path data\alarm.db) { Move-Item data\alarm.db* data\guardia\ }
   New-Item -ItemType Directory data\alarm.db | Out-Null
   if (Test-Path .\spool) { Move-Item .\spool .\spool.off }
   New-Item -ItemType File .\spool | Out-Null
   ```

   Se apartan también `alarm.db-wal` y `alarm.db-shm` si existen, para que la
   restauración no mezcle ficheros de dos bases distintas. El buzón se inutiliza
   dejando un **fichero** llamado `spool` donde el código espera una carpeta: el
   `append` falla igual que con el disco lleno. Antes de seguir, comprobar que
   `.\spool` es un fichero y no una carpeta, porque si no el escenario no prueba
   nada.

2. Abrir la aplicación y armar. Debe aparecer el aviso rojo
   `REGISTRO NO PERSISTENTE`, la barra de estado debe decir que el historial
   está en memoria y **no** debe salir ningún `ackevt`: la placa conserva los
   eventos y, con **Ver → Mostrar latidos**, se ve `buf` subiendo con cada
   detección. Las filas que se vean en la ventana son solo de esa sesión.
3. Cerrar la aplicación y restaurar el estado:

   ```powershell
   Remove-Item data\alarm.db
   Move-Item data\guardia\* data\
   Remove-Item data\guardia
   Remove-Item .\spool
   if (Test-Path .\spool.off) { Move-Item .\spool.off .\spool }
   ```

4. Volver a abrir la aplicación y comprobar que el historial vuelve a crecer y
   que los eventos retenidos en la placa se reenvían y se acusan.

**Evidencia.** `Get-ChildItem .\spool` antes y después; `integrity_check`
devolviendo `ok`; el listado de `triggered` con una fila por disparo; el detalle
de la fila con la duración de la sirena; y, en HW-05D, el aviso
`REGISTRO NO PERSISTENTE` junto con un `hb` que muestre `buf` subiendo.

**Criterio de aceptación.**

- 3 de 3 repeticiones sin evento perdido ni efecto repetido.
- `PRAGMA integrity_check` devuelve `ok` y la base abre con la ventana.
- `.\spool` queda vacío (o con solo los ficheros de la ejecución en curso) y
  no aparecen eventos huérfanos en el historial.
- Tras el corte en plena sirena, al abrir la aplicación el estado persistido
  retoma lo que quedaba en vez de arrancar desarmada; no se manda un segundo
  aviso por el mismo disparo.
- En 05D, con SQLite y el buzón inutilizados a la vez, la ventana muestra
  `REGISTRO NO PERSISTENTE` y **no** hay `ackevt`: la placa conserva el evento y
  el latido lo delata con `buf` creciente. Al restaurar los ficheros, los
  eventos retenidos se reenvían y se acusan sin duplicar efectos. Las pruebas
  unitarias de `tests/test_storage.py` y `tests/test_spool.py` cubren la lógica,
  pero no sustituyen a esta comprobación en placa.

**Resultado.**

| Resultado | Fecha | Operador | Evidencia |
|---|---|---|---|
| pendiente (repetición a) | | | |
| pendiente (repetición b) | | | |
| pendiente (repetición c) | | | |
| pendiente (HW-05D) | | | |

### HW-06 — Sirena, retardos y FSM sobre la placa

**Objetivo.** Verificar con el oído y con el registro que la sirena dura lo
configurado, que se corta cuando debe y que la FSM real recorre
`ARMANDO → ARMADA → ENTRADA → DISPARADA → REARMANDO → ARMADA` sin depender del
simulador.

**Precondiciones.** **HW-03A** en `PASS` (HW-03B, `BLOQUEADO`, no aplica). Plazos
y sirena acortados para no estar dos minutos por repetición: **Retardos y
rearme…** (`Ctrl+Alt+R`) con salida y entrada de 10 s, rearme activo con espera
de 1 min y 2 intentos; **Zumbador…** (`Ctrl+Alt+B`) con `siren_ms` de 10 s.

**Procedimiento.**

1. Armar (`Ctrl+Mayús+A`): el hero muestra `ARMANDO` con reloj y la placa pita
   cada vez más rápido; a los ~10 s pasa a `ARMADA`, con voz y pitido `armed`.
2. Entrar en la estancia estando armada: `ENTRADA` con reloj de 10 s. Desarmar
   dentro del plazo y comprobar que **no** se dispara.
3. Repetir y dejar vencer el plazo: `DISPARADA`, sirena de 10 s, fila roja
   **Alarma disparada** con la duración y aviso por los canales activos.
4. Con la alarma disparada, silenciar (`Ctrl+Mayús+M`): la sirena calla en el
   acto y la espera de rearme cuenta desde ese momento.
5. Dejar que rearme: `REARMANDO` con cuenta atrás y, al vencer, `ARMADA` con la
   fila **Rearmada tras disparo**. Repetir hasta agotar los 2 intentos:
   **Rearme automático agotado** y estado `DISPARADA` fijo.
6. Durante la sirena, **Desconectar** (menú Archivo → Desconectar; no tiene
   atajo) y volver a **Conectar** (`Ctrl+R`, que ya cierra y reabre el puerto):
   eso provoca un reinicio por DTR y un `hello` nuevo. La sirena debe retomarse
   por lo que quedaba, no reiniciarse a 10 s.
7. Con la sirena sonando, cerrar la ventana: la placa debe callar sola al
   cumplirse `siren_ms`, sin depender del PC.

**Evidencia.** Cronómetro o marcas de tiempo de los logs para cada transición;
filas del historial con sus etiquetas reales —`alarm` en todas las transiciones
de estado, armado y desarmado incluidos, `triggered` en el disparo, `rearming`
en «Sirena terminada», `rearmed` en «Rearmada tras disparo» y `error` en
«Rearme automático agotado»— y el `hello` del reinicio del paso 6. El disparo se
localiza luego con `alarmctl log --tag triggered`. Ojo: `armed` y `disarmed` son
claves de voz, nombres de patrón y estados de pantalla, **no** etiquetas del
historial.

**Criterio de aceptación.**

- Sirena medida dentro de ±1 s del valor configurado, y nunca por encima de
  `siren_ms`.
- Silenciar, desarmar y `stop` cortan la sirena al instante (≤1 s).
- El paso de `ARMANDO` a `ARMADA` ocurre al vencer el plazo, sin pitidos de
  salida en el rearme.
- El reinicio por DTR no calla una alarma disparada ni reinicia el contador de
  rearmes; el `hello` con `boot_id` nuevo dispara la retoma de la sirena.
- Cerrar la ventana con la sirena en curso no la deja sonando más allá de
  `siren_ms`.

**Resultado.**

| Resultado | Fecha | Operador | Evidencia |
|---|---|---|---|
| pendiente (parcial: pasos 1-3 y el corte por desarme del paso 4) | 2026-11-14 00:23-00:28 | jvelasca | `data\validacion\HW-06-20261114-0025-fsm.log`, `HW-06-20261114-0029-fsm-ronda2.log` |

Lo medido, con las marcas de tiempo del historial (configuración de la sesión:
salida 10 s, entrada 10 s, `siren_ms` 10 s, rearme 60 s con 2 intentos):

| Paso | Medición | Resultado |
|---|---|---|
| 1, `DESARMADA → ARMANDO → ARMADA` | 00:26:50.260 → 00:27:00.371 = **10,11 s** | Cumple |
| 1 (repetición) | 00:27:50.333 → 00:28:00.369 = **10,04 s** | Cumple |
| 2, `ENTRADA` desarmada dentro del plazo | 00:27:22.075 → 00:27:28.118 = 6,04 s, sin `triggered` | Cumple |
| 3 (ronda 1), `ENTRADA → DISPARADA` | 00:23:25.168 → 00:23:35.352 = **10,18 s** | Cumple |
| 3 (ronda 1), sirena completa | 00:23:35.352 → 00:23:45.353 = **10,00 s** | Cumple, dentro de ±1 s |
| 5, rearme | 00:23:45.353 → 00:24:45.353 = **60,000 s** | Cumple, cuenta desde el fin de la sirena |
| 3 (ronda 2), `ENTRADA → DISPARADA` | 00:28:08.387 → 00:28:18.372 = **9,99 s** | Cumple |
| 3 (ronda 2), sirena cortada por desarme | 00:28:18.372 → 00:28:21.915 = **3,54 s** | Se cortó al desarmar, como debe |

`REARMANDO → ARMADA` dejó la fila `rearmed` «Rearmada tras disparo» con «rearme 1
de 2», y el disparo la fila `triggered` con «sirena 10 s · rearme 1 de 2 en
1 min». Las etiquetas del historial son `alarm`, `triggered`, `rearming` y
`rearmed`, como dice §6.

**Pendiente de este escenario:** silenciar con `Ctrl+Shift+M` (paso 4) y
comprobar que la espera de rearme cuenta desde el silencio; agotar los 2
rearmes hasta `Rearme automático agotado` (paso 5); la retoma de la sirena tras
un reinicio por DTR (paso 6); y cerrar la ventana con la sirena en curso
(paso 7). Sin esos cuatro puntos el escenario **no** se marca `PASS`.

**Observación de la sesión:** con los plazos acortados, el enfriamiento de
avisos (`cooldown_s` 180) suprime correo y push de los armados y desarmados
seguidos y deja filas `bounce` (««Alarma armada» bloqueado por enfriamiento o
cola llena»). La voz y el pitido suenan igual: lo que se bloquea es el aviso
remoto. No es un `FAIL`, es el límite de F4 que `00-PREMISAS.md` §11 documenta,
pero conviene saberlo antes de contar avisos en esta campaña.

### HW-07 — Watchdog e identidad de arranque (observacional)

**Objetivo.** Registrar lo que la placa dice de sus arranques reales, sin
fabricar un bloqueo.

**Precondiciones.** HW-02 y HW-04 ejecutados (ya hay varios reinicios).

**Procedimiento.**

1. Anotar `cfg.wdt_rst` en cada `hello` capturado en HW-02, HW-04 y HW-06.
2. Anotar el motivo real de reinicio: `cfg.wdt_rst` en el `hello` y la línea
   `sesion nueva: boot_id=<n> antes=<m> motivo=power|watchdog` que el gestor
   escribe en la **consola** —la aplicación no tiene log de fichero, solo
   `data/arranque.log` con los fallos de arranque—. `power` cubre el arranque
   normal, el tirón del USB y el reinicio por DTR; `watchdog` solo aparece si el
   arranque anterior lo provocó el perro de 8 s.
3. Provocar un tirón de USB y comparar: debe seguir siendo `power`, porque el
   watchdog no se dispara.
4. **No** se fuerza un bloqueo del firmware. Forzarlo exige modificar el sketch
   o cortocircuitar el bucle, y ninguna de las dos cosas cabe en una campaña de
   validación de una versión cerrada.

**Evidencia.** Extractos de `hello` con `wdt_rst` y la etiqueta de motivo de
reinicio.

**Criterio de aceptación.**

- Todos los arranques observados informan `wdt_rst: 0` y motivo `power`.
- El campo `boot_id` sube en cada arranque y no se repite entre sesiones.
- **Estado del escenario:** observacional. La recuperación real del watchdog no
  se certifica en esta campaña —no se fuerza un bloqueo y no se habrá visto un
  `wdt_rst: 1`—, así que su resultado se registra como `BLOQUEADO`, nunca como
  `PASS`. Mientras no se ejecute, la fila sigue *pendiente*.

**Resultado.**

| Resultado | Fecha | Operador | Evidencia |
|---|---|---|---|
| pendiente (su resultado será `BLOQUEADO`, no `PASS`) | | | |

### HW-08 — Operación continua (opcional, no bloqueante)

**Objetivo.** Cubrir el criterio de larga duración de F4
([`00-PREMISAS.md`](00-PREMISAS.md) §11): 72 h sin caídas ni fugas y un
desenchufe de 1 h que el sistema absorba solo.

**Precondiciones.** HW-01 a HW-06 en `PASS`, salvo HW-03B, que consta como
`BLOQUEADO` con su motivo. Equipo en su ubicación definitiva, PC que no se vaya
a usar para otra cosa durante la prueba.

**Procedimiento.**

1. Dejar la aplicación y la placa funcionando 72 h con el registro rotando.
2. A la mitad, desenchufar el Arduino durante 1 h y volver a enchufarlo.
3. Anotar: reinicios de la aplicación, errores en el log, tamaño del histórico,
   número de reconexiones y si el aviso de pérdida de enlace apareció y
   desapareció solo.
4. Revisar que no haya procesos duplicados ni crecimiento anómalo del uso de
   memoria del `.exe`/Python.

**Evidencia.** Log de la aplicación de las 72 h, captura del estado al final y
resumen de reconexiones.

**Criterio de aceptación.** Sin caídas de la aplicación, reconexión automática
tras el desenchufe, sin avisos duplicados y con el histórico íntegro. Se marca
como **opcional**: su ausencia no invalida el resto de la campaña, pero sí deja
sin cubrir el criterio de F4.

**Resultado.**

| Resultado | Fecha | Operador | Evidencia |
|---|---|---|---|
| pendiente | | | |

---

## 7. Tabla resumen

| ID | Escenario | Fecha | Operador | Placa / puerto | Firmware | Resultado | Evidencia |
|---|---|---|---|---|---|---|---|
| HW-01 | Compilación reproducible | 2026-11-13 23:24 | jvelasca | — (compilación, sin placa) | 1.7.0 | **PASS** | `data\validacion\HW-01-20261113-2324-compile.log` |
| HW-02 | Flasheo, sesión y saludo | 2026-11-14 00:14 | jvelasca | `COM3` | 1.7.0 | **PASS** | `data\validacion\HW-02-20261114-0014-hello-{a,b}.log`, `HW-02-20261114-0015-skipwarmup.log` |
| HW-03A | Órdenes y patrones del zumbador | 2026-11-14 00:19 | jvelasca | `COM3` | 1.7.0 | **PASS** (rechazo con `buzzer=0`: **BLOQUEADO**) | `data\validacion\HW-03A-20261114-0019-patrones.log` |
| HW-03B | `err` por `badop`/`oversize` | 2026-11-13 | jvelasca | — | 1.7.0 | **BLOQUEADO** | — sin remitente de líneas crudas (§6 HW-03B) |
| HW-04A | Tirón de USB (reinicio) | | | | | pendiente | |
| HW-04B | Pérdida de enlace con placa viva | | | | | pendiente (**BLOQUEADO** esperado con CH340) | |
| HW-04C | Desbordamiento del buffer | | | | | pendiente | |
| HW-05 | Corte de alimentación del PC | | | | | pendiente | |
| HW-05D | Fallo simultáneo de SQLite y buzón | | | | | pendiente | |
| HW-06 | Sirena y FSM reales | 2026-11-14 00:28 | jvelasca | `COM3` | 1.7.0 | pendiente (parcial: retardos, disparo, sirena de 10 s y un rearme medidos) | `data\validacion\HW-06-20261114-0025-fsm.log`, `HW-06-20261114-0029-fsm-ronda2.log` |
| HW-07 | Watchdog (observacional) | | | | | pendiente (su resultado será **BLOQUEADO**) | |
| HW-08 | 72 h (opcional) | | | | | pendiente | |

Ninguna de las filas pre-declaradas cuenta como ejecutada: HW-03B y el rechazo
del zumbador constan `BLOQUEADO` porque ya se sabe que las herramientas actuales
no pueden provocarlos, no porque se hayan intentado.

## 8. Registro de incidencias

Cada `FAIL` abre una fila aquí **antes** de intentar arreglarlo, y su corrección
va en un PR aparte.

| ID | Escenario | Fecha | Síntoma | Evidencia | Estado | PR de corrección |
|---|---|---|---|---|---|---|
| | | | | | | |

No se admiten como cierre de una incidencia «ya funciona en el simulador» ni
«lo cubre una prueba unitaria»: si el fallo se vio en la placa, se cierra en la
placa.

## 9. Límites de esta campaña

- **Una sola placa y un solo sensor.** No se valida el comportamiento entre
  unidades ni la dispersión de los HC-SR501.
- **Buffer de 16 en RAM.** No hay almacenamiento duradero a bordo: un corte de
  alimentación pierde los eventos sin acusar y reinicia `lost`. El spool del PC
  —`.\spool`, en la raíz de datos— cubre el fallo del disco, no el de la placa.
- **Sin inyección de fallos automatizada.** Desconexiones, cortes y reinicios se
  hacen a mano; no hay un banco que los repita mil veces.
- **HW-03B bloqueado.** No existe remitente de líneas crudas, así que `badop` y
  `oversize` no se pueden provocar desde las herramientas actuales. Por el mismo
  motivo queda bloqueado el rechazo del patrón con `buzzer=0`: el interruptor
  del zumbador vive en RAM y cada apertura del puerto lo repone.
- **HW-07 no concluyente.** El watchdog no se fuerza sin tocar el firmware, así
  que su resultado será `BLOQUEADO` y no certifica la recuperación real.
- **El piezo no es una sirena de potencia.** Se valida que suena y calla cuando
  toca, no que se oiga desde la calle.
- **Sin SAI.** Un corte de luz deja el sistema mudo; es un límite conocido del
  conjunto, no un objeto de esta campaña.
- **Los logs no se versionan.** `data\` está en `.gitignore`; la evidencia vive
  en el equipo de banco y en los extractos que se copian aquí.

## 10. Qué falta para poder certificar el hardware

El PASS de software ya está emitido y no cambia. La certificación de hardware
requiere, como mínimo:

- HW-01 a HW-06 en `PASS` —HW-03B es la única excepción admitida, `BLOQUEADO` y
  motivada—, con sus filas y su evidencia.
- HW-07 registrado con su resultado real (`BLOQUEADO` mientras no se fuerce el
  watchdog).
- Cero incidencias abiertas, o todas con su PR y su revalidación en placa.
- HW-08 ejecutado si se quiere cubrir el criterio de F4.

La formulación que se puede sostener mientras falte algo de lo anterior es la
misma que la del cierre de software:

> Versión 0.1.5 integrada en `main`, revisión de software cerrada y CI en verde.
> Certificación de hardware pendiente: compilación, flasheo y órdenes del
> zumbador superados (HW-01, HW-02 y HW-03A `PASS`), comportamiento físico de la
> FSM, del buffer y de la durabilidad sin ejecutar.

Cuando la campaña termine, será este documento —y no una impresión— lo que
permita cambiar esa frase.
