# Historial de versiones

Todas las versiones notables de este proyecto se documentan aquí. El formato
sigue [Keep a Changelog](https://keepachangelog.com/es/1.1.0/) y la numeración
[SemVer](https://semver.org/lang/es/): `MAYOR.MENOR.PARCHE`.

- **MAYOR**: cambia algo que rompe compatibilidad (formato de la base, del
  protocolo serie o de la copia de seguridad).
- **MENOR**: funcionalidad nueva que no rompe nada.
- **PARCHE**: correcciones y ajustes que no añaden funcionalidad.

## [Sin publicar]

### Por hacer

- Zonas, panel web y bandeja del sistema (F2-F3 de la hoja de ruta).

## [0.1.4] — 2026-11-13

Corrige de verdad los tres hallazgos que 0.1.3 daba por cerrados (H-01, H-02 y
H-03). La entrada de 0.1.3 afirma que los reenvíos se descartaban por
`(boot, seq)`; eso **no estaba implementado**: el único filtro era
`seq <= last_seq_acked`, se reiniciaba en cada apertura de puerto y `boot` valía
~0 en todos los arranques. No había identidad de sesión, el ACK se daba aunque
SQLite fallara y la máquina de estados solo recuperaba `ARMADA`/`DESARMADA`.
Esta versión lo sustituye por completo.

### Corregido

- **H-01 — el ACK exige una escritura durable.** La ruta de `MotionEvent` ya no
  pasa por `_log_event()`: `_handle_motion()` intenta una transacción en SQLite
  (evento + clave + snapshot de la FSM) y, si falla, escribe el mismo registro en
  un buzón JSONL con `flush()` + `os.fsync()`. Solo entonces aplica los efectos
  y confirma a la placa. Si ni la base ni el buzón aceptan la escritura **no hay
  ACK**: la placa conserva el evento en su buffer y lo reenvía.
- **H-02 — identidad de arranque real en la EEPROM.** El firmware 1.7.0 guarda
  un contador `boot_id` de 32 bits en dos ranuras alternas con magic y CRC16 y lo
  anuncia en el `hello`. Se escribe una vez por arranque, nunca en `loop()`.
  Python compone `(boot_id, seq)` y la apunta en la nueva tabla
  `processed_events`: un reenvío del mismo evento se vuelve a confirmar pero no
  alimenta otra vez la FSM ni lanza un segundo aviso. Un `seq=1` de un arranque
  nuevo se procesa aunque el anterior también tuviera `seq=1`.
- **H-02 — el gestor distingue sesiones.** Sin `boot_id` (firmware 1.6.0 o
  anterior) se conserva la heurística por `boot` y se marca la sesión como no
  fiable. Con él, abrir el puerto ya no reinicia el acuse "por si acaso": lo
  reinicia el `hello` cuando el `boot_id` es otro, y lo pide `replay` una vez por
  sesión. Un `hb.up_ms` que retrocede delata un reinicio por watchdog que no se
  vio y fuerza un `hello` nuevo. Los eventos que llegan antes del saludo se
  aparcan y se despachan con la identidad correcta.
- **H-02 — el ACK viaja con su sesión.** `ack_events(seq, boot_id)` ignora
  acuses de una sesión que ya no es la actual: si la placa se reinició entre
  procesar y confirmar, confirmar el `seq` viejo borraría eventos de la sesión
  nueva.
- **H-03 — la FSM se recupera entera, no solo armada/desarmada.** `to_snapshot()`
  y `from_snapshot()` cubren los seis estados y guardan los plazos que quedaban.
  Al arrancar se descuenta el tiempo que estuvo cerrada la aplicación: un
  `PENDING` que venció mientras el PC estaba apagado pasa a `TRIGGERED`; un
  `ARMING` o un `REARMING` vencidos pasan a `ARMED`; un `TRIGGERED` sigue
  disparado y la sirena se reanuda con el tiempo que le quedaba, no con uno
  nuevo entero.
- **H-03 — el estado se marca como guardado solo tras el commit.** El snapshot
  se escribe junto a `armed_intent` y `armed_at` en un único
  `set_state_many()`. Si el commit falla no se actualiza la huella en memoria, de
  modo que el siguiente cambio lo vuelve a intentar en vez de dar por bueno un
  estado que no está en disco.

### Añadido

- **Buzón durable (`data/spool/`).** Ficheros JSONL por día con `fsync`,
  reimportación idempotente al arrancar y cuarentena `.bad` para un fichero
  corrupto. No sustituye a la prueba física: solo cubre el caso de SQLite
  bloqueado, disco lleno o carpeta de solo lectura.
- **Aviso permanente de registro no persistente.** La tarjeta del dispositivo
  muestra `REGISTRO NO PERSISTENTE` con el motivo (historial en memoria, base
  bloqueada, eventos en el buzón o eventos sin guardar) mientras dure.
- **`fw 1.7.0`**: `boot_id` en EEPROM y en el `hello`. `FW_VERSION` sube.

### Endurecido durante la auditoría de la propia 0.1.4

Revisando el código con la lista de comprobaciones en la mano aparecieron dos
agujeros que esta misma entrada cierra, antes de publicar:

- **Transacciones explícitas con `ROLLBACK`.** `EventStore` abre y cierra sus
  escrituras con `BEGIN IMMEDIATE`/`COMMIT` en vez de confiar en el `BEGIN`
  implícito de `sqlite3`. Si el `INSERT` del historial o del estado fallaba a
  mitad, la clave de `processed_events` quedaba pendiente en la conexión y el
  siguiente commit ajeno la confirmaba: el evento se daba por procesado sin
  estar guardado y su reenvío se descartaba como duplicado.
- **El buzón tolera una línea a medias.** Un corte durante la escritura deja el
  fichero sin salto final; el `append` siguiente separa con un salto para no
  pegarse al fragmento roto (antes el registro nuevo quedaba dentro de la línea
  inválida y se perdía). En la reimportación, las líneas buenas entran al
  historial y solo las rotas se apartan en el `.bad`, que se acumula en vez de
  pisarse: antes un solo byte malo dejaba fuera todos los eventos del fichero.


### Notas de compatibilidad

- Con firmware 1.6.0 la aplicación sigue funcionando, pero la deduplicación entre
  reconexiones no puede garantizarse (no hay identidad de sesión). Se avisa en el
  registro.
- La base de datos gana la tabla `processed_events`; se crea sola, no hay
  migración que ejecutar.

## [0.1.3] — 2026-11-13

Cierre de los cinco hallazgos de prioridad alta y media de la auditoría externa
(H-01 a H-05). No añade funcionalidades: cambia el orden en que se confirman los
eventos, hace durable el estado de armado, acota el handshake y deja de gastar
cuotas y credenciales donde no tocaba.

### Corregido

- **El evento se confirma después de procesarlo y guardarlo.** El ACK ya no lo
  da el hilo lector al recibir: lo pide la ventana con `ack_events(seq)` tras
  registrar la fila y aplicar la transición, y lo escribe el hilo lector. Si el
  PC se cierra en ese hueco, el evento sigue en el buffer de la placa y se
  recupera con `cmd replay` al reconectar. Los reenvíos se descartan por
  `(boot, seq)` y se cuentan como duplicados.
- **El estado de armado sobrevive al reinicio.** Nueva tabla `system_state` en
  el historial y opción `alarm.restore_state_on_start` (activada por defecto).
  Lo guardado manda sobre `armed_on_start`; al arrancar se registra «Estado
  recuperado: ARMADA desde …» y el primer `hello` reenvía `armed=1`.
- **La pantalla distingue estar armada de poder vigilar.** El hero muestra
  `ARMADA · VIGILANCIA OPERATIVA`, `ARMADA · SENSOR CALENTÁNDOSE` o
  `ARMADA · SENSOR NO DISPONIBLE` según saludo, calentamiento y salud del
  enlace. El estado lógico armado no se toca para no abrir un hueco.
- **Un puerto mudo ya no bloquea la autodetección.** Nueva opción
  `serial.handshake_timeout_s` (3 s): si el puerto abre y no llega el saludo, se
  cierra, se descarta dos minutos y se prueba el siguiente candidato. Un `COM`
  fijado a mano se reintenta igual. El fallback de detección exige PID y
  descripción.
- **Descartar un aviso por cola llena ya no consume la cuota.** `Notifier.submit`
  comprueba el hueco antes de pasar la compuerta y hace ambas cosas bajo un
  cerrojo: un descarte no bloquea el siguiente aviso de la misma causa.
- **`ayuda` responde sin credenciales.** Es la única orden que se acepta sin
  clave ni TOTP, con todas las órdenes desactivadas y durante un bloqueo, y no
  cuenta como intento fallido. No revela estado ni ejecuta nada.

## [0.1.2] — 2026-11-13

Revisión previa a una auditoría externa: el firmware gana el watchdog que la
documentación prometía desde F1, y la documentación deja de describir un
firmware que no era el que está escrito.

### Añadido

- **Watchdog de 8 s en la placa.** `setup()` lee y limpia `MCUSR`, desactiva el
  perro antes de nada (la receta que evita el bucle de reinicios del gestor de
  arranque) y lo arma al final; `loop()` lo alimenta en su primera línea. Si el
  bucle se cuelga, la placa se reinicia sola en vez de quedarse muda, y el PC ve
  desaparecer el latido y volver el `hello`.
- **`cfg.wdt_rst` en el `hello`.** Vale 1 cuando el arranque anterior lo provocó
  el watchdog y 0 en cualquier otro caso, para distinguir «se cayó y se levantó
  sola» de «alguien la desconectó». Documentado en `doc/02-PROTOCOLO.md`.
- **Sección «Límites conocidos»** en `doc/00-PREMISAS.md` §4.5: sin SAI ni
  batería, sin supervisión de línea, piezo en vez de sirena de potencia, sin
  teclado físico, una sola zona y el watchdog del PC entendido como latido.
- **Pruebas de contrato del sketch**: watchdog armado y alimentado, prohibición
  de `delay()`, `String`, `malloc` y `new`, y comprobación de que `EVT_CAP` y
  `CMD_CAP` coinciden con los números publicados en el protocolo.

### Corregido

- **`doc/00-PREMISAS.md` §4.4 describía otro firmware.** Decía 32 eventos
  (son 16), 256 bytes de línea (son 96 en el downlink), configuración en EEPROM
  (vive en RAM y el PC la reenvía en cada handshake, ADR-024), LED con parpadeo
  por estado de enlace (parpadea al calentar y después copia el PIR) e
  interrupción en `RISING` y `FALLING` (es `CHANGE`). Corregido en §4.2, §4.4 y
  §9.3, y anotado el consumo real: 15 404 bytes de flash (6 %) y 1 018 de RAM
  (12 %) con el watchdog dentro.
- Versión del firmware a **1.6.0**.

## [0.1.1] — 2026-11-13

Primera ejecución de la integración continua. No cambia nada de lo que hace la
aplicación: arregla dos cosas que solo se ven al compilar en otro equipo.

### Corregido

- **Las pruebas de `alarmctl` ya no dependen de la página de códigos del
  equipo.** El proceso hijo escribía con la codificación de la consola (cp1252,
  cp850…) y la prueba leía UTF-8, así que los acentos y la raya rompían doce
  pruebas según la máquina. Ahora el hijo se lanza siempre en UTF-8.
- **`alarmctl` no se corta si la consola no sabe dibujar un carácter.** En un
  `cmd.exe` de fábrica en español no existen la raya ni las comillas
  tipográficas: el comando moría a mitad de la tabla con `UnicodeEncodeError`.
  Se sustituye el carácter suelto por `?` y la salida sigue adelante.

## [0.1.0] — 2026-11-13

Primera versión publicada: alarma de presencia completa en un solo PC, con el
Mega como sensor y la aplicación de escritorio como central.

### Añadido

- **Enlace serie con el Arduino Mega** — protocolo v1 con ACK, reintentos y
  `seq`; reconexión automática y aviso de pérdida del enlace. Incluye enlace
  simulado (`--sim`) para trabajar sin hardware.
- **Estado de la alarma con retardos** — armado con retardo de salida, retardo
  de entrada con pitidos acelerados, disparo con sirena, silencio manual y
  rearme automático tras un disparo, con enfriamiento y límite de avisos por
  hora.
- **Avisos por cuatro canales** — voz por evento configurable (con caché de
  audio), correo SMTP, SMS con Twilio y push gratuito al móvil con ntfy.
  Notificador común con reintentos, resultado por canal y `ignore_gate` para los
  avisos que no deben silenciarse.
- **Mando remoto por ntfy** — órdenes `armar`, `desarmar`, `consultar`,
  `silenciar`, `ayuda` y `panico` (desactivada por defecto) desde el tema de
  mando, con respuesta al móvil. Autenticación por clave de mando o TOTP
  (RFC 6238 en biblioteca estándar, con anti-repetición), bloqueo tras varios
  fallos y descarte de las órdenes anteriores al arranque.
- **Interfaz PyQt6** — ventana principal con tarjeta de estado, botón de
  conexión, panel del sensor con barras de señal, historial en vivo con
  exportación a CSV, seis indicadores de canal y tema propio. Diálogos de voz,
  correo/SMS/móvil, mando remoto, retardos y rearme, zumbador y ajuste del
  sensor.
- **Historial persistente en SQLite** — detecciones, cambios de estado y
  resultados de aviso, con retención configurable y exportación a CSV.
- **Zumbador de la placa** — patrones por evento y aceleración de los pitidos
  en los retardos.
- **Copia de seguridad y traslado** — exportación e importación de la
  configuración y las credenciales en un solo fichero, y paquete en un `.exe`
  independiente con PyInstaller.
- **Consola `alarmctl`** — `info`, `calibrate`, `beep`, `notify-test`,
  `ntfy-test`, `remote-send`, `remote-listen` y `backup` (exportar/importar).
- **Documentación** — premisas y hoja de ruta, protocolo serie, avisos e
  interfaz, con capturas generadas desde los propios diálogos.
- **Pruebas y calidad** — 501 pruebas de `pytest`, `ruff` y `mypy` en modo
  estricto, comprobados en integración continua.

[Sin publicar]: https://github.com/jvelasca/alarma-casa-arduino/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/jvelasca/alarma-casa-arduino/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/jvelasca/alarma-casa-arduino/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/jvelasca/alarma-casa-arduino/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jvelasca/alarma-casa-arduino/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jvelasca/alarma-casa-arduino/releases/tag/v0.1.0
