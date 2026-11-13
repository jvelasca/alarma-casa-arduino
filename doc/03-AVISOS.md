# Avisos: voz, correo, SMS, móvil, mando remoto y zumbador

Referencia de la capa de avisos. Cubre qué se configura, dónde se guarda, cómo
se prueba y qué límites tiene. Desde F1.4 toda la configuración de avisos se
edita también desde la ventana (**Herramientas → Configuración de voces** y
**Avisos → Correo, SMS y móvil**), sin tocar el TOML a mano.

---

## 1. Alcance

| Pieza | Qué hace | Donde vive |
|---|---|---|
| Estado de alarma | `DESARMADA` / `ARMANDO` / `ARMADA` / `ENTRADA` / `DISPARADA` (+ silenciada) | [`src/alarm/core/state.py`](../src/alarm/core/state.py) |
| Voz | Lee mensajes por el altavoz del PC, con retardo y repeticiones por evento | [`src/alarm/services/tts.py`](../src/alarm/services/tts.py) |
| Correo | Envía avisos por SMTP a uno o varios destinatarios | [`src/alarm/services/email_sender.py`](../src/alarm/services/email_sender.py) |
| SMS | Twilio por HTTP o pasarela correo-a-SMS | [`src/alarm/services/sms_sender.py`](../src/alarm/services/sms_sender.py) |
| Móvil | Notificaciones push gratuitas por ntfy (Android e iPhone) | [`src/alarm/services/ntfy_sender.py`](../src/alarm/services/ntfy_sender.py) |
| Mando remoto | Órdenes desde el móvil por ntfy (`armar`, `desarmar`, `consultar`, `silenciar`) con clave o TOTP | [`src/alarm/services/ntfy_commands.py`](../src/alarm/services/ntfy_commands.py) |
| Orquestación | Cola, filtrado por evento, enfriamiento y límites | [`src/alarm/services/notifier.py`](../src/alarm/services/notifier.py) |
| Historial | Registro persistente en SQLite con retención y exportación CSV | [`src/alarm/services/storage.py`](../src/alarm/services/storage.py) |
| Guardado | Capa local del TOML y `.env` atómico | [`src/alarm/settings_store.py`](../src/alarm/settings_store.py) |
| Zumbador | Pitidos locales en la placa, con cadencias aceleradas | [`firmware/mega-pir/mega-pir.ino`](../firmware/mega-pir/mega-pir.ino) |

Los retardos de entrada y salida funcionan como en una central de mercado:
`DISARMADA → ARMANDO → ARMADA` al armar, y `ARMADA → ENTRADA → DISPARADA`
al detectar movimiento. Durante `ARMANDO` el PIR no vigila (se está saliendo)
y durante `ENTRADA` hay margen para desarmar. Los dos plazos se configuran y
la placa los marca con pitidos cada vez más rápidos.

---

## 2. Configuración

### 2.1 Ficheros

| Fichero | Se versiona | Contenido |
|---|---|---|
| `config/alarm.example.toml` | Si | Plantilla comentada de toda la configuración |
| `config/alarm.toml` | **No** | Tu configuración base. Se crea copiando el ejemplo en el primer arranque |
| `config/alarm.local.toml` | **No** | Solo lo que cambias desde los diálogos. La app nunca reescribe `alarm.toml` |
| `.env.example` | Si | Plantilla de credenciales |
| `.env` | **No** | Usuario y contraseña SMTP; SID y token de Twilio; token de ntfy; clave y secreto TOTP del mando |

Precedencia: valores por defecto < `config/alarm.toml` < `config/alarm.local.toml`
< variables de entorno.

La separación en dos capas existe para no perder los comentarios del fichero
base: si el diálogo reescribiera `alarm.toml`, la plantilla dejaría de ser
legible. Los secretos no entran en ninguna de las dos capas.

Si el TOML tiene un error, la aplicación no arranca a medias: muestra el
mensaje concreto y no abre la ventana. Es lo contrario de arrancar sin voz y
descubrirlo cuando pasa algo.

**Qué llega a `alarm.local.toml`.** Además de lo que guardan los diálogos de
correo, SMS, móvil y voz, las casillas de los menús escriben en esa capa en el
mismo momento en que se marcan:

| Casilla | Clave que se guarda |
|---|---|
| Avisos → Voz por el altavoz | `tts.enabled` |
| Avisos → Aviso por correo | `notify.email.enabled` |
| Avisos → Aviso por SMS | `notify.sms.enabled` |
| Avisos → Aviso al móvil (ntfy) | `notify.ntfy.enabled` |
| Ver → Mostrar latidos | `ui.show_heartbeats` |
| Ver → Pitidos al ajustar | `ui.beeps` |

Antes estas casillas solo cambiaban el objeto en memoria: al pulsar
**Avisos → Recargar configuración** —o al cerrar y volver a abrir— `load_config`
devolvía `false` y el check se desmarcaba solo. Ahora `_persist` escribe la capa
local y aplica el cambio; si la escritura falla, revierte el check y avisa.
El check **Simulado** no se guarda: es un modo de sesión, no configuración.

**Qué queda fuera del volcado JSON.** **Archivo → Guardar configuración en
JSON…** (`Ctrl+Mayús+C`) escribe toda la configuración en un `.json` legible,
pensado para consultarla o adjuntarla a un informe. Nunca incluye secretos:
`email.user`, `email.password`, `sms.token`, `ntfy.token`, `remote.key` y
`remote.totp_secret` salen en blanco y se listan en `credenciales_omitidas`. Sí
se conservan destinatarios, remitente, SID de Twilio, números, servidor, tema de
ntfy y el tema de mando, que es lo que se necesita para diagnosticar un aviso
que no llega. Tampoco salen los valores de `.env`: el JSON
se construye desde `AppConfig`, y los tokens y la contraseña solo existen ya
resueltos en memoria.

**Para llevarse los secretos a otro PC** hay un fichero aparte:
**Archivo → Guardar copia completa (con credenciales)…** (`Ctrl+Mayús+B`) crea un
`.alarmbackup` con la configuración entera **más** las siete claves de `.env`
que tengan valor (`ALARM_SMTP_USER`, `ALARM_SMTP_PASSWORD`, `ALARM_TWILIO_SID`,
`ALARM_TWILIO_TOKEN`, `ALARM_NTFY_TOKEN`, `ALARM_REMOTE_KEY` y
`ALARM_REMOTE_TOTP`). En el equipo nuevo, **Archivo → Restaurar copia
completa…** escribe la capa local, actualiza esas claves en `.env` conservando
comentarios y claves ajenas, y recarga la configuración sin reiniciar. El
fichero no va cifrado: trátalo como una contraseña. Ver
[`doc/04-INTERFAZ.md`](04-INTERFAZ.md#2-menús) y
[`README.md`](../README.md#instalar-en-otro-pc).

### 2.2 Bloques

```toml
[serial]
port = "auto"            # autodetección por VID/PID, o "COM5"
baudrate = 115200
auto_connect = true      # conectar sola al arrancar y mantener el enlace
reconnect_max_s = 30     # espera máxima entre reintentos (retroceso exponencial)
heartbeat_timeout_s = 8  # sin datos durante 8 s: enlace perdido (4 latidos)

[alarm]
armed_on_start = false   # arrancar ya armada
cooldown_s = 180         # mínimo entre avisos de la misma causa
max_per_hour = 6         # tope total de avisos por hora
exit_delay_s = 45        # armar -> vigilar; pitidos acelerados (0 = sin retardo)
entry_delay_s = 30       # detectar -> disparar; margen para desarmar (0 = al instante)
auto_rearm = false       # rearmarse solo tras un disparo
rearm_delay_s = 300      # espera desde que calla la sirena hasta rearmar
max_rearms = 3           # tope de rearmes seguidos (1 .. 10)

[tts]
enabled = true
voice = "Microsoft Helena Desktop"
rate = 0                 # -10 lento .. +10 rápido
volume = 90              # 0 .. 100

[tts.events.armed]       # un bloque por evento
enabled = true
text = "Alarma armada."
label = "ARMADA"         # lo que se pinta en el hero
repeat = 2               # 1 .. 10 (0 = no se dice)
gap_ms = 0               # pausa entre repeticiones (0 .. 30000)
delay_ms = 0             # 0 .. 120000
log = true               # deja fila «Voz» en el registro

[tts.events.disarmed]
enabled = true
text = "Alarma desarmada."
label = "DESARMADA"
repeat = 1
delay_ms = 0

[tts.events.triggered]
enabled = true
text = "Atención. Se ha detectado una presencia a las {hora}."
label = "DISPARADA"
repeat = 2
delay_ms = 0

[tts.events.device_lost]
enabled = true
text = "Atención. El Arduino se ha desconectado: la alarma no está disponible."
label = "SIN SENSOR"
repeat = 2
gap_ms = 0
delay_ms = 0
log = true

[ui.labels]              # etiquetas de pantalla, independientes de la voz
armed = "ARMADA"
disarmed = "DESARMADA"
triggered = "DISPARADA"

[notify.email]
enabled = true
on_armed = true
on_disarmed = false
on_triggered = true
on_device_lost = true   # avisar si se pierde el enlace con la placa
host = "smtp.gmail.com"
port = 587
security = "starttls"    # starttls | ssl | none
from = "alarma@example.com"
to = ["yo@example.com", "otra@example.com"]
subject = "[Alarma] {evento} a las {hora}"
timeout_s = 15
retries = 2

[notify.sms]
enabled = false
provider = "twilio"      # twilio | email_gateway
on_armed = false
on_disarmed = false
on_triggered = true
on_device_lost = true    # avisar si se pierde el enlace con la placa
to = ["+34600000000"]
from_number = ""         # remitente Twilio
gateway_domain = ""      # dominio de la pasarela correo-a-SMS
template = "[Alarma] {evento} a las {hora}"
timeout_s = 10
retries = 1

[notify.ntfy]
enabled = false
server = "https://ntfy.sh"       # o tu instancia: https://ntfy.tudominio.com
topic = ""                       # 1-64 de [A-Za-z0-9_-]; largo y aleatorio
on_armed = false
on_disarmed = false
on_triggered = true
on_device_lost = true
title = "[Alarma] {evento}"      # {evento} -> DISPARADA, armada, desarmada...
priority_triggered = "urgent"    # min | low | default | high | urgent
priority_device_lost = "high"
priority_armed = "default"
priority_disarmed = "default"
timeout_s = 10
retries = 1
# Token de un servidor propio: en .env como ALARM_NTFY_TOKEN, nunca aqui.

[remote]
enabled = false                  # escuchar ordenes del movil
topic = ""                       # vacio: "<notify.ntfy.topic>-mando"
allow_arm = true
allow_disarm = true
allow_status = true
allow_silence = true
allow_panic = false              # hace sonar la sirena al instante
reply = true                     # contestar al movil con el resultado
lockout_failures = 5             # 0 = sin bloqueo; 1 .. 50
lockout_minutes = 15             # 1 .. 1440
# La clave y el secreto TOTP van en .env: ALARM_REMOTE_KEY y
# ALARM_REMOTE_TOTP. Nunca en este fichero.

[buzzer]
enabled = true
pin = 11                # informativo: el real es PIN_BUZZER del firmware
siren_ms = 120000       # 2 min de sirena; tope 600000 (10 min)

[log]
max_events = 100000      # tope de filas guardadas
max_age_days = 365       # antigüedad máxima
visible_rows = 1000      # filas que se muestran en la ventana
db_path = "data/alarm.db"
```

Los textos admiten los marcadores `{hora}`, `{evento}`, `{zona}` y `{duracion}`.
Una plantilla con un marcador desconocido no rompe el aviso: se registra y se
usa el texto por defecto.

El bloque antiguo `[tts.messages]` sigue aceptándose: si no hay
`[tts.events.*]`, se migra en memoria al cargar. Así un `alarm.toml` de F1.3
sigue funcionando sin tocarlo.

---

## 3. Voz

Se usa `System.Speech` de .NET, invocado con PowerShell sin ventana, y
`winsound` para reproducir. Ambas cosas vienen con Windows y CPython: no hay
nada que instalar.

- La síntesis ocurre **una sola vez por texto**: el WAV queda en `data/tts/` y
  los siguientes avisos lo reproducen desde caché. La clave incluye voz,
  velocidad y volumen, así que cambiar la voz no reproduce una caché vieja.
- Cada evento trae su propio **retardo** y sus propias **repeticiones**. Un
  aviso con retardo no adelanta a uno inmediato: la cola se ordena por
  vencimiento.
- La cola tiene profundidad 6. Si se llena, se descarta el aviso más antiguo:
  uno de hace diez segundos ya no interesa.
- `cancel_pending()` anula los avisos que aún no han sonado al cambiar de
  estado o al silenciar. Es lo que evita oír «presencia detectada» tres
  segundos después de haber desarmado.
- Un fallo de audio se registra y no interrumpe la recepción de eventos.

### 3.1 Ajustar cada mensaje

**Herramientas → Configuración de voces…** (`Ctrl+Alt+V`, también en **Avisos**)
abre una sola ventana con una tabla: una fila por aviso y una columna por
decisión. Arriba, el bloque de voz común; debajo, los seis mensajes.

| Campo | Para qué |
|---|---|
| Activo | Silencia solo ese evento sin tocar los demás |
| Mensaje | Frase completa que se lee; admite `{hora}`, `{evento}`, `{zona}`, `{duracion}` |
| Etiqueta | Texto corto del hero; solo armada, desarmada y disparada |
| Repes | 0–10 veces; 0 equivale a no decirlo |
| Retardo | 0–120 s de espera antes de la primera palabra |
| Intervalo | 0–30 s de pausa entre una repetición y la siguiente |
| Registrar | Si la locución deja una fila «Voz» en el registro |

La tabla muestra una **vista previa** de la fila seleccionada con la plantilla
resuelta, y tiene **Probar** (habla ahora, sin guardar), **Restaurar todo**
(vuelve a los valores de fábrica) y **Guardar** (escribe en
`alarm.local.toml`). Si un aviso activo se queda sin texto, guardar avisa en
lugar de dejar un mensaje mudo.

Los tres tiempos son independientes a propósito: el **retardo** evita pisar la
sirena, el **intervalo** separa las repeticiones para que se entiendan, y
**Repes** decide la insistencia. Un ejemplo razonable para «disparada» es
3 repeticiones, retardo 2 s e intervalo 1,5 s; para «armada», 1 repetición y
sin retardo.

El bloque superior elige la voz instalada del sistema, la velocidad (−10 a +10),
el volumen (0–100) y la activación global. La lista de voces sale de SAPI y se
consulta una sola vez por sesión: si la consulta falla, el desplegable queda
vacío pero la ventana abre igual y se puede escribir el nombre a mano.

**Registrar** solo afecta al historial, no al funcionamiento: el cambio de
estado y el evento «Sin sensor» se apuntan siempre, pero la frase hablada no
tiene por qué repetirse en el registro. Desmarcado, se dice igual y no se
escribe la fila «Voz».

### 3.2 Elegir voz a mano

```powershell
Add-Type -AssemblyName System.Speech
(New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() |
    ForEach-Object { $_.VoiceInfo.Name }
```

Copia el nombre exacto en `voice`. Si la voz no existe, se usa la del sistema
sin fallar.

---

## 4. Correo

### 4.1 Gmail

1. Activa la verificación en dos pasos en la cuenta de Google.
2. Cuenta de Google → Seguridad → Contraseñas de aplicaciones → crea una para
   «Correo».
3. Copia la contraseña de 16 caracteres en `.env`:

```dotenv
ALARM_SMTP_USER=cuenta@gmail.com
ALARM_SMTP_PASSWORD=xxxxxxxxxxxxxxxx
```

La contraseña normal de la cuenta **no** funciona y el error que devuelve el
servidor no lo explica bien: usa siempre la de aplicación. Google la muestra en
cuatro grupos de cuatro caracteres; al pegarla en el diálogo, quita los
espacios (son 16 caracteres seguidos). Si se cuelan, la aplicación los quita
sola cuando detecta ese patrón exacto de 16 caracteres y deja el resto de
contraseñas intactas.

### 4.2 Varios destinatarios

`to` es una lista. El diálogo permite añadir y quitar direcciones con
validación básica (`algo@dominio`), y **no** guarda nada hasta que todas son
válidas. El asunto y el cuerpo admiten los mismos marcadores que la voz.

### 4.3 Otros servidores

| Servidor | host | puerto | security |
|---|---|---|---|
| Gmail | `smtp.gmail.com` | 587 | `starttls` |
| Outlook / Microsoft 365 | `smtp.office365.com` | 587 | `starttls` |
| Servidor propio con SSL | el tuyo | 465 | `ssl` |
| Servidor local de pruebas | `127.0.0.1` | 1025 | `none` |

El puerto manda sobre el ajuste guardado: 465 y 994 se abren con SSL desde el
primer byte, y 587, 25, 2525 y 1025 con STARTTLS. Si el TOML trae una pareja
imposible (465 con `starttls`, o 587 con `ssl`), el canal usa el cifrado que el
puerto admite y lo anota en el log. El diálogo además sincroniza ambos campos:
elegir «SSL (465)» pone el puerto 465, y escribir 587 vuelve a STARTTLS.

### 4.4 Qué se protege

- La contraseña solo se lee de `.env`, que está en `.gitignore`, y el campo del
  diálogo aparece vacío con el texto «sin cambios»: si no lo tocas, no se
  reescribe.
- Los errores se sanean antes de escribirse en el log: si el mensaje del
  servidor contuviera la contraseña, se sustituye por `***`.
- Cada intento tiene timeout explícito; un servidor que no contesta no deja el
  hilo de avisos colgado.
- La única salida deliberada de un secreto es la **copia completa**
  (`.alarmbackup`): la pide el usuario por su nombre, avisa de que lleva las
  contraseñas en claro y no se escribe en ningún sitio sin ese gesto.

### 4.5 «Connection unexpectedly closed» al probar

Ese texto es el que aparece cuando el servidor corta la sesión, y casi siempre
significa que **el usuario o la contraseña no se han aceptado**. La cadena de
autenticación de `smtplib` prueba varios mecanismos: Gmail responde `535` al
primero y cierra la conexión al ver el segundo, de modo que el motivo real
—«Username and Password not accepted»— desaparecía.

El canal hace ahora **un solo intento** con el mecanismo que el servidor anuncia
(`PLAIN` o `LOGIN`) y no reintenta credenciales rechazadas. La ventana de prueba
dice en qué etapa falló y qué revisar:

| Etapa | Significado | Qué hacer |
|---|---|---|
| `conexión` | No se pudo abrir la sesión o el servidor cortó al saludar | Puertos 587/465 y firewall de la red |
| `TLS` | El servidor rechazó el cifrado | 587 con STARTTLS, 465 con SSL |
| `autenticación` | Usuario o contraseña rechazados | En Gmail, contraseña de aplicación sin espacios; el remitente debe ser esa misma cuenta |
| `envío` | Se autenticó pero rechazó el mensaje | Revisa remitente y destinatarios |

En Gmail, el orden de comprobación es: verificación en dos pasos activa,
contraseña de aplicación recién creada para «Correo», los 16 caracteres sin
espacios, y `from` igual a la cuenta que se autentica.

---

## 5. SMS

Hay dos proveedores y se elige con `provider`:

| Proveedor | Cómo funciona | Cuándo usarlo |
|---|---|---|
| `twilio` | POST a `api.twilio.com` con `urllib` y autenticación básica | Es el que funciona hoy y el valor por defecto |
| `email_gateway` | Reutiliza el SMTP y envía a `<numero>@<dominio>` | Solo si tu operador mantiene pasarela correo-a-SMS |

### 5.1 Twilio

Sí, Twilio exige una cuenta propia: no hay forma de mandar un SMS real sin un
proveedor. Los pasos, en orden:

1. Crea la cuenta en [console.twilio.com](https://console.twilio.com) y añade
   crédito (la cuenta de prueba da un saldo inicial limitado).
2. Compra un número con capacidad de SMS, o verifica tu móvil si vas a usar la
   cuenta de prueba.
3. Copia el **Account SID** y el **Auth Token** del panel de inicio. El SID
   empieza por `AC`; el token es el que está oculto tras «Show».
4. Pon el remitente en `[notify.sms].from_number` (formato `+34…`): el número
   comprado o, en la prueba, el verificado.
5. Guarda las credenciales en `.env`:

```dotenv
ALARM_TWILIO_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALARM_TWILIO_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Coste y límites.** Un SMS a España cuesta unos 0,08 €, más el alquiler
mensual del número. La cuenta de prueba **solo entrega a números verificados en
Twilio**: si el destinatario no está verificado, el envío falla aunque el código
sea correcto. Para uso real hay que pasar la cuenta a pago.

**La aplicación te dice qué falta.** El diálogo **Avisos → Correo, SMS y móvil…**,
pestaña SMS, muestra un estado en vivo («Listo para probar» o «Falta: cuenta
SID, token de autenticación y número remitente») y valida antes de probar. El
botón **Abrir consola de Twilio** lleva directo al panel. Un número tecleado sin
pulsar *Añadir* también cuenta al probar o guardar.

Si Twilio contesta con un error, el detalle del proveedor se muestra tal cual
(código y mensaje): es lo que permite distinguir «número no verificado» de
«saldo agotado».

No se usa el SDK oficial: es un POST de formulario y `urllib` ya viene con
Python. El cuerpo se recorta a **160 caracteres** antes de enviarlo, sin cortar
a mitad de palabra, porque un SMS largo se factura como varios y se lee peor.

Cada número recibe su propio mensaje: un fallo con uno no impide intentarlo con
el siguiente, y el resultado dice a cuántos se entregó.

### 5.2 Pasarela correo-a-SMS

Pon el dominio del operador en `gateway_domain`. El canal manda un correo por
número a `34600111222@dominio`. Si el dominio está vacío, el canal lo dice al
probar y no intenta enviar: es preferible un error claro a un envío a la nada.

**Aviso honesto:** la mayoría de operadores españoles han retirado o limitado
estas pasarelas. Están implementadas porque son gratuitas cuando funcionan,
pero no se asume que existan.

### 5.3 Números

Se normalizan a un formato E.164 aproximado: `600 11 22 33`, `0034600112233`
y `+34 600 11 22 33` acaban todos en `+34600112233`. Sin prefijo se asume
España; un `+` inicial siempre se respeta.

### 5.4 Probar

El diálogo **Avisos → Correo, SMS y móvil** tiene una pestaña por canal y un
botón **Probar** en cada una. La prueba corre en el hilo del servicio y el
resultado vuelve por una señal Qt: la ventana nunca se queda congelada esperando
al servidor. El resultado se muestra en un modal pequeño con el motivo si falla
y la opción de reintentar.

Las pruebas manuales ignoran el enfriamiento y el tope horario: el usuario ha
pedido el envío, así que no tiene sentido bloquearlo.

---

## 6. Móvil con ntfy

Notificaciones push gratuitas al teléfono, sin SMS, sin tarifa adicional y sin
tarjeta SIM especial. Funciona igual en Android (Google Play, F-Droid) y en
iPhone (App Store). En el PC no se instala nada: el canal usa solo `urllib` de
la biblioteca estándar.

```
Arduino → PC → POST HTTPS al servidor ntfy → app del móvil
```

| Pieza | Valor |
|---|---|
| Servidor por defecto | `https://ntfy.sh` (público y gratuito) |
| Servidor propio | cualquier instancia de ntfy, con token si exige acceso |
| Tema | nombre de 1 a 64 caracteres de `[A-Za-z0-9_-]` |
| Transporte | JSON a la raíz del servidor (el tema viaja en el cuerpo) |
| Dependencias | ninguna: `urllib` y `json` |

### 6.1 Instalar y suscribirse

1. Instala la app **ntfy** en el móvil.
2. En el PC: **Avisos → Correo, SMS y móvil…**, pestaña **Móvil (ntfy)**.
3. Pulsa **Generar tema seguro**: rellena un nombre largo y aleatorio
   (`alarma-…`, 21 caracteres) que hace de secreto compartido.
4. En la app, suscribe un tema nuevo con ese nombre exacto, en el servidor
   `ntfy.sh`.
5. Pulsa **Probar móvil**. Debe llegar en segundos, con el teléfono bloqueado y
   la app cerrada.
6. Marca **Enviar avisos al móvil (ntfy)** y guarda.

El tema se compara carácter a carácter: un guion bajo de más y no coincide.

### 6.2 Cómo se envía

Se publica un JSON a la raíz del servidor, no cabeceras HTTP. En una cabecera no
cabe un acento sin codificarlo, y el título lo lleva; en el cuerpo, no hay
sorpresas:

```json
{
  "topic": "alarma-9fj2k_x7qp1a",
  "title": "[Alarma] DISPARADA",
  "message": "Se ha detectado una presencia...\n\nHora: 22:14:03",
  "priority": 5,
  "tags": ["rotating_light"]
}
```

Con servidor propio y token, se añade `Authorization: Bearer <token>`.

| Evento | Prioridad por defecto | Etiqueta |
|---|---|---|
| `triggered` | `urgent` (5) | `rotating_light` |
| `device_lost` | `high` (4) | `warning` |
| `armed` | `default` (3) | `lock` |
| `disarmed` | `default` (3) | `unlock` |

`urgent` hace sonar el móvil aunque esté en silencio; el resto respeta el modo
del teléfono. Cada prioridad se ajusta en el diálogo o en `[notify.ntfy]`.

El mensaje se recorta a 3.900 caracteres (el servidor admite 4 KB) y un 4xx no
se reintenta: tema inexistente, token rechazado o cuerpo demasiado grande no
mejoran repitiendo. Un fallo de red o un 5xx sí.

### 6.3 Seguridad

En **ntfy.sh** un tema **no es privado**: cualquiera que conozca el nombre puede
suscribirse y publicar. HTTPS protege el transporte, no el tema.

| Modalidad | Protección |
|---|---|
| ntfy.sh + tema largo y aleatorio | solo el secreto del nombre |
| Servidor propio + token (`ALARM_NTFY_TOKEN`) | control de acceso real |

El diálogo avisa en ámbar si el tema parece corto o si publica en `ntfy.sh` sin
token. Avisa, no impide: un tema corto es una decisión, no un error de sintaxis.
El token va a `.env`, nunca al TOML, y se sanea en los mensajes de error: una
excepción de `urllib` puede arrastrar la petición y el token no debe acabar en el
registro.

### 6.4 Límites

- Sin Internet no hay notificación. Ni ntfy, ni correo, ni SMS salen sin red; la
  sirena y la voz siguen funcionando.
- El canal obedece los mismos límites que los demás (`cooldown_s`,
  `max_per_hour`): ver la sección 9, «Límites de aviso».
- En iPhone, la entrega depende de los servicios de notificaciones de Apple y de
  que la app tenga permiso. En Android puede recibir en segundo plano.
- Un tema por instalación: todos los móviles suscritos reciben lo mismo. Para
  avisar a varias personas, cada una se suscribe al mismo tema.

### 6.5 Probar sin abrir la ventana

```powershell
.\.venv\Scripts\python.exe tools\alarmctl.py ntfy-test
```

Muestra servidor, tema, si hay token y la prioridad, y envía una notificación de
prueba con la configuración real. Sale con código 1 si falta el tema o si el
servidor la rechaza.

---

## 7. Mando remoto por ntfy

La app del móvil no solo recibe avisos: también puede dar órdenes. El PC escucha
un **segundo tema**, `alarma-xxx-mando`, y ejecuta lo que llegue —armar,
desarmar, consultar o silenciar— contestando por el tema de avisos. El canal de
avisos y el de mando son independientes a propósito: apagar las notificaciones
del móvil no deja la alarma sorda a las órdenes, y un tema comprometido no
obliga a cambiar el otro.

```
Movil -> PC:  clave-7f3k9x armar        (tema alarma-xxx-mando)
PC -> Movil:  [Mando] Armar
              Armada. Vigila en 45 s (retardo de salida).
              Hora: 08:12:33 · Estado: ARMANDO   (tema alarma-xxx)
```

### 7.1 Qué se necesita

- El canal **Móvil (ntfy)** activo y con servidor y tema: el mando reutiliza el
  mismo token y la misma conexión. Si se apaga «Aviso al móvil (ntfy)», el mando
  remoto también se detiene, y el diálogo lo dice al marcar la casilla.
- Una **clave de mando** de 12 caracteres o más, o un **secreto TOTP**. Sin
  ninguna de las dos, la casilla no se puede activar: una alarma que obedece a
  cualquiera que acierte el tema no es una alarma.
- El tema de mando debe caber en 64 caracteres. Si el tema de avisos es corto,
  se deriva `alarma-xxx-mando`; si no cabe, la ventana pide uno explícito.

### 7.2 Activarlo

1. En el PC: **Avisos → Mando remoto (ntfy)…**.
2. Marca **Aceptar órdenes por ntfy**.
3. Pulsa **Generar clave** (o escribe una propia) y cópiala al móvil.
4. Revisa el **tema de mando** y suscríbete a él en la app, en el mismo servidor
   que el tema de avisos.
5. En la app, abre ese tema y **publica** el mensaje con la clave y la orden.
6. Guarda. El indicador `llave` de la barra de estado se pone verde.

El diálogo muestra el tema de mando efectivo aunque se haya derivado, con botón
para copiarlo, y un panel en vivo con lo que está pasando: si escucha, desde
cuándo, la última orden con su resultado, contadores de recibidas, aceptadas y
rechazadas, y el último error. La tabla de las últimas 20 órdenes es solo de la
sesión; lo que queda para siempre es la fila del historial.

### 7.3 Gramática de las órdenes

Se ignoran mayúsculas, acentos y la puntuación sobrante. La clave puede ir antes
o después del verbo, y el verbo en cualquier posición del mensaje.

| Orden | Alias | Qué hace |
|---|---|---|
| `armar` | `activar`, `arm` | Arma: `ARMANDO` con retardo de salida o `ARMADA` si no lo hay |
| `desarmar` | `apagar`, `disarm` | Desarma, cancela un retardo de entrada y calla la sirena |
| `consultar` | `estado`, `status` | Responde con estado, enlace, sensor, sirena, retardos y los últimos 3 eventos |
| `silenciar` | `callar`, `silencio` | Calla la sirena; la alarma sigue armada y vigilando |
| `ayuda` | `help`, `?` | Lista las órdenes y ejemplos, sin revelar la clave |
| `panico` | `panic` | Dispara la sirena ya, sin esperar al sensor. **Desactivado por defecto** |

```text
clave-7f3k9x armar
ARMAR CLAVE-7F3K9X
clave-7f3k9x consultar
```

Un mensaje sin verbo conocido se ignora en silencio: el tema puede recibir
mensajes de prueba y no todos merecen una respuesta. Un mensaje **con** verbo
pero sin credencial válida tampoco recibe respuesta —no se confirma a un
desconocido que el tema existe—, queda en el historial sin la clave y cuenta
para el bloqueo.

### 7.4 Clave o TOTP

- **Clave de mando** (`ALARM_REMOTE_KEY` en `.env`): se compara con
  `hmac.compare_digest`, sin distinguir espacios sobrantes. Mínimo 12
  caracteres; «Generar clave» produce 32 aleatorios.
- **TOTP** (`ALARM_REMOTE_TOTP`): código de 6 dígitos, 30 s por paso, ventana de
  ±1 paso y **anti-repetición** (un código ya aceptado no vale dos veces en su
  mismo paso). Se genera con `secrets.token_bytes(20)` y se copia a Google
  Authenticator, Authy o similar con la URI `otpauth://` que muestra el diálogo;
  no hay QR para no añadir dependencias.

Con TOTP configurado **la clave deja de aceptarse**. Es deliberado: si la clave
siguiera valiendo, filtrarla abriría la puerta igualmente y el segundo factor no
serviría de nada. El reloj del PC debe estar en hora; un desfase de más de 30 s
hace que todos los códigos se rechacen.

**Bloqueo por fallos.** Tras `lockout_failures` intentos fallidos (5 por
defecto; 0 desactiva el bloqueo) se ignoran las órdenes durante
`lockout_minutes` (15 por defecto) y se envía **un solo** aviso `[Alarma] Mando
bloqueado` al tema de avisos. El contador se reinicia al reiniciar la app.

### 7.5 Respuestas

Cada orden aceptada contesta al tema de avisos con título `[Mando] <orden>`,
etiqueta `mando-reply` y `white_check_mark`, y el resultado real: «Armada.
Vigila en 45 s», «Desarmada», «No se hizo nada: la alarma ya estaba armada».
Un fallo contesta `[Mando] No se pudo` con etiqueta `x` y prioridad alta. La
etiqueta `mando-reply` y el prefijo del título son también el filtro que impide
que el PC se lea a sí mismo y entre en bucle.

La orden `consultar` usa el mismo formato que el botón **Enviar estado al
móvil** del diálogo:

```text
Estado: ARMADA
Enlace: conectado
Sensor: sin movimiento (12 s)
Retardos: salida 45 s · entrada 30 s
Canales: voz, móvil

Últimos eventos:
· 08:12:33  Armada
· 08:10:02  Desarmada

Hora: 08:14:07
```

### 7.6 Seguridad

| Escenario | Qué protege |
|---|---|
| ntfy.sh con tema largo y aleatorio | solo el secreto del nombre del tema |
| ntfy.sh + clave de mando | la clave viaja en el mensaje y **cualquiera que lea el tema la ve** |
| ntfy.sh + TOTP | el código caduca en 30 s; leerlo no basta para reutilizarlo |
| Servidor propio + token + ACL | control de acceso real, usuario y contraseña |

- En **ntfy.sh** el tema no es privado. El diálogo avisa en ámbar si se activa
  el mando con clave y sin TOTP en el servicio público.
- En un servidor propio, limita el tema por usuario:
  `ntfy access alarma alarma-xxx-mando rw` y `ntfy access movil alarma-xxx r`.
  El token va en `.env` (`ALARM_NTFY_TOKEN`), nunca en el TOML.
- **Rotar** es cambiar la clave o el secreto TOTP en el diálogo y guardar: el
  listener se reinicia y la credencial anterior deja de valer al instante.
- Nunca se escribe la clave ni el código en el historial; los errores de red se
  sanean con `sanitize` para que el token no acabe en el registro.

### 7.7 Límites

- **El PC tiene que estar encendido** y con la app abierta. Con el equipo
  apagado la orden no se ejecuta: no hay nada escuchando.
- **Nada anterior al arranque se ejecuta.** El cursor de lectura empieza en el
  instante en que arranca la escucha, así que un `desarmar` de ayer no se aplica
  hoy al abrir la app. Si llega una orden con el PC apagado, se pierde; si llega
  durante un corte de red, se recupera al reconectar.
- No hay modos `stay`/`away`/`night`, zonas ni omisión de sensores. Hay un solo
  PIR: fingir esos modos daría una falsa sensación de control.
- La entrega depende de ntfy y de la red. Sin Internet no llegan órdenes, igual
  que no salen avisos; la sirena y la voz siguen funcionando en local.

### 7.8 Diagnóstico

```powershell
# Escucha el tema de mando sin ejecutar nada y dice si la clave o el TOTP valen.
.\.venv\Scripts\python.exe tools\alarmctl.py remote-listen --seconds 60

# Publica una orden como lo haria el movil, con la configuracion real.
.\.venv\Scripts\python.exe tools\alarmctl.py remote-send --action armar

# Fuerza una clave concreta (util para comprobar rechazos).
.\.venv\Scripts\python.exe tools\alarmctl.py remote-send --action consultar --key 000000
```

`remote-listen` muestra cada mensaje con la hora del servidor y del PC para
detectar desfase de reloj, y no toca el controlador ni el historial. Si el
diálogo dice «Sin conexión», revisa servidor, tema y token; si dice «Faltan
datos», falta la clave o el TOTP, o el canal móvil está apagado.

---

## 8. Pérdida del enlace con la placa

Una alarma que no puede leer el sensor no está protegiendo nada, y callarlo es
peor que decirlo. Por eso la pérdida de enlace es un evento de primera clase.

### 8.1 Cuándo se considera perdido

El firmware late cada 2 s. `DeviceManager` da el enlace por perdido tras
`serial.heartbeat_timeout_s` segundos sin recibir **ninguna** línea válida
(**8 s** por defecto: cuatro latidos perdidos). También cuenta como pérdida que
el puerto desaparezca o que una lectura lance un error.

Antes de eso no hay aviso posible: si nunca llegó un `hello`, la placa puede
simplemente no estar enchufada. En ese caso el botón queda en ámbar
**Reconectando…** —se sigue intentando— pero no se registra una avería, no se
habla y no sale ningún correo ni SMS.

### 8.2 Qué se dispara

Con el enlace ya establecido y sin que el usuario haya pulsado **Desconectar**:

1. **Registro**: fila de severidad `error`, evento `Sin sensor`, detalle
   «Arduino desconectado: la alarma no está disponible». Queda en SQLite, así
   que sobrevive a cerrar la aplicación.
2. **Voz**: `device_lost`, con el texto de `[tts.events.device_lost]`
   («Atención. El Arduino se ha desconectado: la alarma no está disponible.»).
   Si el evento o la voz global están desactivados, simplemente no suena; el
   aviso de «Sin sensor» queda igualmente en el registro.
3. **Correo, SMS y móvil**: causa `device_lost`, que cada canal filtra con su
   casilla **Sin sensor** (`on_device_lost`). Pasa por la misma cola,
   enfriamiento y tope horario que el resto de avisos.
4. **Pantalla**: etiqueta roja en la tarjeta del dispositivo y el hero añade
   «Sin sensor: la alarma no está disponible».

El aviso se emite **una sola vez** por caída: el refresco de 500 ms no repite
correos. La reconexión sigue sola, con retroceso exponencial hasta
`reconnect_max_s`; si la placa reaparece en otro `COMx`, se vuelve a detectar por
VID/PID.

### 8.3 Al recuperar

Cuando vuelve a llegar cualquier línea válida, se registra
**«Sensor recuperado»** y, si está activado, se pronuncia `device_ready`
(«Sensor operativo»). No se manda correo de recuperación: el silencio ya es la
buena noticia y no conviene llenar el móvil.

### 8.4 Desconectar a mano no avisa

**Archivo → Desconectar** marca la parada como intencionada: no genera evento de
error, ni voz, ni correo. El botón vuelve a gris **Conectar**. La distinción vive
en la ventana (`_expect_disconnect`), no en el gestor: es una decisión de
producto, no de protocolo.

### 8.5 La casilla «Sin sensor»

En **Avisos → Correo, SMS y móvil** hay un cuarto evento en cada pestaña.
Marcado, `on_device_lost = true`; desmarcado, ese canal deja de avisar de las
caídas sin tocar los demás eventos.

---

## 9. Límites de aviso

Se aplican en `AlarmController.allow()` y se inyectan en el notificador, de modo
que la regla existe una sola vez:

- **Enfriamiento por causa**: tras un aviso de `triggered`, no sale otro de la
  misma causa durante `cooldown_s`.
- **Tope por hora**: como máximo `max_per_hour` avisos en cualquier ventana de
  60 minutos.
- **Filtro por evento**: cada canal decide con `enabled_for(cause)` si ese
  evento concreto le toca (`on_armed`, `on_disarmed`, `on_triggered`,
  `on_device_lost`). Un canal activo pero sin credenciales tampoco entra en la
  cola.
- Un aviso bloqueado **siempre se registra** en la ventana. Nunca es silencioso.

---

## 10. Zumbador

### 10.1 Cableado

Pieza piezo pasiva de tres patillas o de dos hilos:

```
Zumbador pasivo (+) ────[ 100 Ω ]──── D11 del Mega
Zumbador pasivo (-) ────────────────── GND (el contiguo a D13)
```

- Con patillas a 7,62 mm (tres pasos), el piezo encaja justo entre `D11` y ese
  `GND`: los terminales entran en esos dos agujeros y salvan `D12` y `D13` por
  encima. Forra ese tramo con termorretráctil o cinta: **D13 es el LED
  integrado** y el firmware lo conmuta, así que un roce lo deja sin funcionar
  y carga el pin.
- La **100 Ω** no es obligatoria para un piezo, pero sí recomendable: recorta
  el pico de corriente de cada flanco (el piezo es capacitivo), protege el pin
  y suaviza el tono. Se puede soldar en el propio terminal del zumbador.
- D11 es `OC1A` (Timer1) y también MOSI del SPI. Ninguno de los dos se usa:
  `tone()` genera la onda con el Timer2, así que el PWM del pin da igual y el
  firmware no habla SPI.
- `tone()` usa el Timer2 en el Mega: deshabilita el PWM de D9 y D10. En este
  proyecto no hay PWM, así que no hay conflicto.
- Si el zumbador es **magnético** (lleva electrónica), no lo conectes directo:
  usa un transistor NPN (BC547 o 2N2222), 1 kΩ en la base y un 1N4148 en
  paralelo con el zumbador, alimentado desde 5V.
- No lo alimentes desde 3.3V: sonará muy flojo.
- El pin de `[buzzer].pin` del TOML es **informativo**: el real lo fija
  `PIN_BUZZER` en el firmware y la aplicación no lo envía a la placa.

### 10.2 Patrones

| Nombre | Cuándo suena | Forma |
|---|---|---|
| `armed` | al armar | dos pitidos agudos |
| `disarmed` | al desarmar | un tono grave largo |
| `detect` | detección con la alarma armada | tres pitidos rápidos |
| `alarm` | al dispararse | sirena alterna, acotada por `siren_ms` (2 min por defecto) |
| `error` | error del dispositivo | un tono muy grave |
| `test` | prueba manual | tres tonos ascendentes |
| `exit` | retardo de salida | pitidos de 1000 ms que bajan a 250 ms |
| `entry` | retardo de entrada | pitidos más agudos, de 700 ms que bajan a 150 ms |
| `stop` | desarmar o cancelar | corta la sirena o la cuenta atrás en curso |

El pitido de detección lo genera **la propia placa**, no el PC: funciona aunque
el USB vaya con retraso o la ventana esté ocupada. Las cadencias de los
retardos también: la placa recibe la duración total y acelera sola, sin que el
PC tenga que mandar un pitido por cada uno. Así el aviso no depende de que la
ventana esté libre justo cuando alguien está saliendo o entrando.

### 10.3 Estado remoto

El PC envía `armed=1` cuando la alarma vigila de verdad y `armed=0` al
desarmar. Con retardo de salida, durante `ARMANDO` llega `armed=0` (se está
saliendo y el PIR vería a la persona); al vencer el plazo se envía `armed=1`.
El firmware solo pita en detección si está armado. Al abrir el puerto por DTR
la placa se reinicia y pierde ese estado; la ventana lo reenvía al recibir
`hello`, sin pitar.

Comandos disponibles:

```
cmd <id> set armed=0|1 buzzer=0|1 siren_ms=120000
cmd <id> beep name=armed|disarmed|detect|alarm|error|test|tone [ms=120000] [freq=2000] [force=1]
```

El zumbador calla durante el calentamiento del PIR. `force=1` salta ese silencio
y lo usan todas las pruebas manuales (ventana y `alarmctl`): sin él, probar el
zumbador recién conectada la placa devolvía `ok=false` y el silencio parecía un
cableado mal puesto.

### 10.4 Probar y ajustar desde la ventana

`Alarma → Zumbador…` (`Ctrl+Alt+B`, también en `Herramientas`) abre el diálogo
de prueba. Reúne lo que antes había que deducir:

- **Tono continuo** de 100–8000 Hz durante 100–10000 ms. Es la comprobación más
  rápida del cableado: si suena, el piezo y el pin están bien.
- **Patrón** concreto (`test`, `armed`, `disarmed`, `detect`, `alarm`, `error`)
  con el mismo sonido que hará la placa en cada situación.
- **Frecuencia de prueba**: los piezo baratos resuenan entre 2 y 4 kHz y fuera de
  esa banda suenan mucho más flojo. Se busca a oído y se guarda.
- **Sirena**: duración del patrón `alarm`, de 1 a 600 s (10 min). Dos minutos por
  defecto: quince segundos se acaban antes de que nadie reaccione, y el tope
  evita que un fallo deje el piezo sonando toda la tarde.
- **Interruptor**: apaga el zumbador de la placa sin tocar la voz ni los avisos
  remotos.

Todo lo que se guarda vive en `config/alarm.local.toml`:

```toml
[buzzer]
enabled = true
pin = 11          # informativo: manda PIN_BUZZER en el firmware
siren_ms = 120000 # 2 minutos
test_hz = 2000    # frecuencia del tono de prueba
test_ms = 1200    # duración del tono de prueba
```

Las pruebas se envían con `force=1`, así que suenan aunque el PIR esté
calentando. Al guardar, la ventana reenvía `set armed/buzzer/siren_ms` a la placa.

### 10.5 Polaridad y montaje

- La **pata larga** (o el terminal marcado con `+`) es el positivo y va al pin
  `D11`. La **pata corta** (lado plano) es el negativo y va a `GND`.
- Aun así, un piezo pasivo **suele sonar también del revés**, solo algo más
  flojo: la polaridad no explica por sí sola un silencio total. Si no se oye
  nada, lo primero es descartar el calentamiento (`force`), el interruptor
  `buzzer=0` y que el zumbador sea realmente pasivo.
- Un zumbador **activo** (con oscilador interno) no sirve en este montaje: con
  `tone()` da clics o suena muy flojo, y si además se conecta al revés puede
  dañarse. Se distingue midiendo con el polímetro: el pasivo da circuito abierto
  (es capacitivo), el activo magnético da unas decenas de ohmios.

---

## 11. Retardos de entrada y salida

### 11.1 Qué hace cada uno

| Retardo | Desde | Hasta | Para qué |
|---|---|---|---|
| Salida (`exit_delay_s`) | pulsar «Armar» | que el PIR vigile | salir de casa sin disparar |
| Entrada (`entry_delay_s`) | detectar movimiento | disparar la sirena | llegar al teclado y desarmar |

Durante el retardo de salida el estado es `ARMANDO` y **no** se vigila: una
detección se registra pero no dispara nada. Durante el de entrada el estado es
`ENTRADA`, la placa sigue armada y las detecciones nuevas **no reinician** el
plazo: quien ya está dentro no puede alargarlo paseándose delante del sensor.

Desarmar en cualquier momento cancela el retardo y deja la alarma en
`DESARMADA`. Si el plazo de entrada vence sin desarmar, se pasa a `DISPARADA`
con todo lo que eso conlleva (sirena, voz, correo, SMS y móvil).

### 11.2 Cuánto duran

Los valores por defecto son los de una central de mercado: **45 s** para salir
y **30 s** para entrar. Se cambian en **Alarma → Retardos de entrada y
salida…** (`Ctrl+Alt+R`) o en el bloque `[alarm]` del TOML. `0` desactiva el
retardo correspondiente: armar vigila al instante o detectar dispara al
instante, como en la versión anterior.

El firmware acota el reloj a los segundos que le llegan; el PC es quien decide
cuándo vence, con un temporizador de 250 ms. La ventana muestra los segundos
que quedan bajo la etiqueta de estado.

### 11.3 Cómo pita

- **Salida:** un pitido cada 1000 ms al principio, que se acelera hasta 250 ms.
- **Entrada:** más agudo y más rápido, de 700 ms a 150 ms. Se distingue del de
  salida de oído, sin mirar la pantalla.
- Al vencer o al desarmar se envía `beep name=stop` y el zumbador calla; si
  vence la salida, suena además el patrón `armed`.

### 11.4 Un solo PIR, una consecuencia

Con una sola zona todo el movimiento entra por el retardo. Eso significa que
quien entre por la puerta también dispondrá de `entry_delay_s` antes de que
suene la sirena. Es el precio de tener un único sensor: en una instalación con
varias zonas, los PIR del recorrido serían «de entrada» y los interiores
dispararían al instante. Si la prioridad es que no haya margen, pon `0` y
asume que no habrá tiempo para desarmar.

### 11.5 Probar la cadencia

En el propio diálogo, **Escuchar salida** y **Escuchar entrada** reproducen
8 segundos de la cadencia real sin armar nada. En consola:

```powershell
.\.venv\Scripts\python.exe tools\alarmctl.py beep --name exit --ms 8000
.\.venv\Scripts\python.exe tools\alarmctl.py beep --name entry --ms 8000
.\.venv\Scripts\python.exe tools\alarmctl.py beep --name stop
```

### 11.6 Rearme automático tras un disparo

Cuando la sirena termina, la alarma puede volver a armarse sola para seguir
vigilando, en vez de quedarse disparada hasta que alguien llegue al teclado.
Es opcional y se ajusta en el mismo diálogo (`Ctrl+Alt+R`) o en `[alarm]`:

| Clave | Por defecto | Qué hace |
|---|---|---|
| `auto_rearm` | `false` | Activa el rearme automático |
| `rearm_delay_s` | `300` | Espera desde que **calla la sirena** hasta volver a vigilar |
| `max_rearms` | `3` | Rearmes seguidos permitidos (1–10) |

La secuencia de tiempos es encadenada, no solapada:

| Momento | Qué ocurre | Estado |
|---|---|---|
| Detección | Suena la sirena `siren_ms` (2 min por defecto) | `DISPARADA` |
| Fin de la sirena | Silencio y cuenta atrás de `rearm_delay_s` | `REARMANDO` |
| Fin de la espera | Vuelve a vigilar y se cuenta el rearme | `ARMADA` |
| Último rearme agotado | Se queda disparada hasta que se desarme | `DISPARADA` |

Ejemplo con sirena de 3 minutos y rearme a los 5: 3 min de sirena, 5 min de
espera y `ARMADA` a los 8 minutos del disparo. La sirena se ajusta en
**Alarma → Zumbador…** (`siren_ms`, 1–600 s).

Detalles que conviene saber:

- **Silenciar adelanta la espera.** La cuenta empieza cuando la sirena calla,
  sea sola o a mano; quien silencia a los 10 s no espera 3 minutos.
- **El rearme va directo a `ARMADA`**, sin retardo de salida: el retardo existe
  para salir de casa, y aquí quien ha disparado ya está dentro.
- **El contador se reinicia al desarmar o armar a mano**, y no se guarda entre
  arranques: tras reiniciar la aplicación se vuelve a avisar.
- **Un reinicio de la placa** con la alarma disparada reenvía la sirena y
  reinicia su plazo, así que la espera tampoco empieza antes de tiempo.

---

## 12. Cómo probarlo

### 12.1 Sin hardware

```powershell
.\scripts\run.ps1
# o
.\.venv\Scripts\python.exe -m alarm --sim
```

Marca «Simulado», arma, pase lo que pase verás el estado cambiar y el registro
de eventos. La voz, el correo, el SMS y el móvil se prueban desde sus diálogos
sin depender del sensor.

### 12.2 Correo, SMS, móvil y mando desde consola

```powershell
.\.venv\Scripts\python.exe tools\alarmctl.py notify-test
.\.venv\Scripts\python.exe tools\alarmctl.py ntfy-test
.\.venv\Scripts\python.exe tools\alarmctl.py remote-listen --seconds 60
.\.venv\Scripts\python.exe tools\alarmctl.py remote-send --action consultar
```

El primero muestra servidor, remitente y destinatarios antes de enviar. El
segundo, servidor, tema, si hay token y la prioridad. `remote-listen` se queda
escuchando el tema de mando y dice si la clave o el TOTP de cada mensaje valen,
sin ejecutar nada; `remote-send` publica una orden como lo haría el móvil. Los
cuatro dicen el motivo si fallan.

### 12.3 Voz

**Herramientas → Configuración de voces… → Probar** (con la fila «Alarma
disparada» seleccionada) habla con el texto actual, aunque no hayas guardado. Si
no suena, revisa que la voz esté activada y el volumen en la misma ventana; el
motivo del fallo queda en el registro.

### 12.4 Zumbador

```powershell
.\scripts\flash.ps1
.\.venv\Scripts\python.exe tools\alarmctl.py beep --name test
.\.venv\Scripts\python.exe tools\alarmctl.py beep --name tone --freq 2700 --ms 1500
.\.venv\Scripts\python.exe tools\alarmctl.py beep --name alarm --ms 3000
.\.venv\Scripts\python.exe tools\alarmctl.py --sim beep --name armed
```

`alarmctl` envía siempre `force=1`, así que la prueba suena aunque la placa
siga calentando; si `ok=false`, el zumbador está apagado en la placa
(`buzzer=0`) o el patrón no existe. Para buscar la frecuencia buena, repite
`--name tone` subiendo `--freq` de 500 en 500 Hz y quédate con la que más suene.

### 12.5 Fallo de red

Prueba con una contraseña incorrecta en `.env`. La ventana **no** debe
congelarse y el error debe aparecer en el registro sin revelar la contraseña,
señalando la etapa `autenticación` y el `535` del servidor.

---

## 13. Fuera de alcance

- Telegram, webhook y notificaciones push.
- SMS entrantes: solo se envía.
- Silenciado con rearme automático y zonas con nombre/severidad.
- Lectura de tarjetas RFID para armar y desarmar: **aplazada**. El diseño está
  esbozado en [`00-PREMISAS.md`](00-PREMISAS.md) §11 «F5.1 — Lector RFID-RC522»; no hay
  código, ni configuración, ni comandos del protocolo.
- Tabla de entregas aparte: el registro guarda el resultado de cada envío, pero
  no hay auditoría por destinatario.
- Sirena de potencia por relé: el zumbador solo señaliza.
