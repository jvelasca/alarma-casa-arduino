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

[Sin publicar]: https://github.com/jvelasca/alarma-casa-arduino/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/jvelasca/alarma-casa-arduino/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jvelasca/alarma-casa-arduino/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jvelasca/alarma-casa-arduino/releases/tag/v0.1.0
