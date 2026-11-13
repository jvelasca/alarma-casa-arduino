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
- **Pruebas y calidad** — 497 pruebas de `pytest`, `ruff` y `mypy` en modo
  estricto, comprobados en integración continua.

[Sin publicar]: https://github.com/jvelasca/alarma-casa-arduino/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jvelasca/alarma-casa-arduino/releases/tag/v0.1.0
