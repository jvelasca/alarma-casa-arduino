/*
 * mega-pir.ino - F1
 *
 * Alarma de presencia: Arduino Mega 2560 + HC-SR501 en D2.
 * Protocolo v1: enlace bidireccional con el PC.
 *
 *   UPLINK   (Arduino -> PC): NDJSON, una linea por objeto.
 *   DOWNLINK (PC -> Arduino): texto plano "cmd <id> <op> [k=v ...]".
 *
 * El uplink es JSON porque lo parsea Python, donde es gratis. El downlink es
 * texto tokenizado porque el AVR tiene 8 KB de RAM y un parser JSON completo
 * cuesta mas de lo que aporta para cuatro comandos. Ver ADR-011.
 *
 * UPLINK
 *   {"t":"hello",...}                       al arrancar y al pedirlo
 *   {"t":"ready","warmup_ms":60000}         fin del calentamiento
 *   {"t":"hb","up":..,"lvl":..,"raw":..,"buf":..,"lost":..,"calib":..}
 *   {"t":"evt","seq":..,"kind":"motion.start","z":1,"ms":..}
 *   {"t":"evt","seq":..,"kind":"motion.end","z":1,"ms":..,"dur":..}
 *   {"t":"evt","seq":..,"kind":"bounce","z":1,"ms":..,"n":..}
 *   {"t":"calib.edge","ms":..,"lvl":..,"dt":..}
 *   {"t":"calib.window","ms":..,"edges":..,"high_ms":..,"duty":..,...}
 *   {"t":"calib.done",...}
 *   {"t":"ack","id":..,"ok":true,"op":".."}
 *   {"t":"err","code":"..","msg":".."}
 *
 * DOWNLINK
 *   cmd <id> ping
 *   cmd <id> hello
 *   cmd <id> set debounce_ms=50 hb_ms=2000 warmup_ms=60000 armed=0 buzzer=1
 *   cmd <id> beep name=armed|disarmed|detect|alarm|error|test|tone [ms=15000] [freq=2000] [force=1]
 *   cmd <id> calib.start window_ms=5000 auto_end_ms=60000
 *   cmd <id> calib.stop
 *   cmd <id> replay
 *   cmd <id> reset
 *   ackevt <seq>                            confirma eventos hasta seq
 *
 * Cableado:
 *   HC-SR501 VCC -> Mega 5V
 *   HC-SR501 OUT -> Mega D2 (INT4)
 *   HC-SR501 GND -> Mega GND
 *   Zumbador pasivo (+) -> Mega D11 (via 100 ohm si es piezo)
 *   Zumbador pasivo (-) -> Mega GND (el de al lado de D13)
 *
 * Sin delay(): todo con millis(). Sin dependencias externas.
 *
 * Watchdog de 8 s: si el bucle se cuelga (una interrupcion que se pierde, una
 * escritura que no vuelve), la placa se reinicia sola en vez de quedarse muda
 * para siempre. El PC ve el latido desaparecer y luego el `hello` de la placa
 * reiniciada, con `cfg.wdt_rst` a 1 en ese primer saludo.
 */

#include <avr/wdt.h>
#include <string.h>

// ---------------------------------------------------------------------------
// Identidad y pines
// ---------------------------------------------------------------------------

const char FW_VERSION[] = "1.6.0";

const uint8_t PIN_PIR = 2;     // INT4 en el Mega
const uint8_t PIN_LED = 13;    // LED integrado
// Zumbador en D11: tone() genera la onda por software sobre Timer2, asi que
// el PWM de este pin (OC1A) no se usa. D11 tambien es MOSI del SPI, que aqui
// no se toca. El piezo se puentea entre D11 y el GND contiguo a D13.
const uint8_t PIN_BUZZER = 11;

// ---------------------------------------------------------------------------
// Configuracion (modificable por comando)
// ---------------------------------------------------------------------------

uint32_t cfgWarmupMs       = 60000UL;
uint32_t cfgDebounceMs     = 50UL;
uint32_t cfgHbMs           = 2000UL;
uint32_t cfgCalibWindowMs  = 5000UL;
uint32_t cfgCalibAutoEndMs = 60000UL;

// ---------------------------------------------------------------------------
// Buffer circular de eventos pendientes de acuse
//
// Se declara antes de cualquier funcion porque el preprocesador de Arduino
// inserta los prototipos al principio del fichero: si el tipo aparece despues,
// los prototipos no compilan.
// ---------------------------------------------------------------------------

enum EvtKind : uint8_t { EVT_START = 0, EVT_END = 1, EVT_BOUNCE = 2 };

struct Event {
  uint32_t seq;
  uint32_t ms;
  uint16_t dur;
  uint8_t  kind;
};

const uint8_t EVT_CAP = 16;
Event    evtBuf[EVT_CAP];
uint8_t  evtHead  = 0;
uint8_t  evtCount = 0;
uint32_t evtSeq   = 0;
uint32_t evtLost  = 0;

// ---------------------------------------------------------------------------
// ISR: solo cuenta y sella el instante del flanco
// ---------------------------------------------------------------------------

volatile uint32_t g_rawEdges  = 0;
volatile uint32_t g_isrLastMs = 0;
volatile uint32_t g_isrDtMs   = 0;

void onPirEdge() {
  const uint32_t now = millis();
  g_isrDtMs   = now - g_isrLastMs;
  g_isrLastMs = now;
  ++g_rawEdges;
}

// ---------------------------------------------------------------------------
// Estado principal
// ---------------------------------------------------------------------------

bool     g_warmupDone  = false;
bool     g_stableLevel = false;
bool     g_changeArmed = false;
uint32_t g_changeMs    = 0;
uint32_t g_bootMs      = 0;
uint32_t g_activityAt  = 0;
uint32_t g_tLastHb     = 0;
uint32_t g_tLastWarmup = 0;
uint32_t g_tLastBlink  = 0;
bool     g_ledState    = false;
bool     g_armed       = false;  // lo fija el PC: solo pita si esta armado
bool     g_buzzerOn    = true;   // interruptor independiente del PC
// El arranque anterior termino por el watchdog, no por un reset normal, un
// tiron del USB o abrir el puerto. Solo se puede saber en el primer hello.
bool     g_wdtReset    = false;
uint32_t g_rawBaseline = 0;
uint32_t g_accepted    = 0;
uint32_t g_noiseSent   = 0;
uint32_t g_noiseCand   = 0;
uint32_t g_noiseCandMs = 0;

// ---------------------------------------------------------------------------
// Estado de calibracion
// ---------------------------------------------------------------------------

bool     g_calibOn          = false;
uint32_t g_calibStartMs     = 0;
uint32_t g_calibWinStart    = 0;
uint32_t g_calibTickMs      = 0;
uint32_t g_calibRiseMs      = 0;
bool     g_calibHasRise     = false;

// ventana en curso
uint32_t wEdges    = 0;
uint32_t wHigh     = 0;
uint32_t wMinGap   = 0;
uint32_t wMaxGap   = 0;
uint32_t wNoiseAt  = 0;

// totales de la sesion
uint32_t tEdges    = 0;
uint32_t tHigh     = 0;
uint32_t tMinGap   = 0;
uint32_t tMaxGap   = 0;
uint32_t tNoiseAt  = 0;
uint32_t tGaps     = 0;

// ---------------------------------------------------------------------------
// Entrada de comandos
// ---------------------------------------------------------------------------

const uint8_t CMD_CAP = 96;
char    cmdBuf[CMD_CAP];
uint8_t cmdLen = 0;

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------

uint32_t clampU32(uint32_t v, uint32_t lo, uint32_t hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

uint32_t readRawEdges() {
  noInterrupts();
  const uint32_t v = g_rawEdges;
  interrupts();
  return v;
}

// Flancos crudos que no corresponden a ningun cambio confirmado.
uint32_t currentNoise() {
  const uint32_t since = readRawEdges() - g_rawBaseline;
  return (since > g_accepted) ? (since - g_accepted) : 0;
}

// Ruido aparecido desde una referencia. Nunca resta: el ruido acumulado puede
// BAJAR cuando el antirrebote confirma un flanco, y una resta sin proteccion
// daria 4294967295 en lugar de 0.
uint32_t noiseSince(uint32_t base) {
  const uint32_t now = currentNoise();
  return (now >= base) ? (now - base) : 0;
}

bool readPir() {
  return digitalRead(PIN_PIR) == HIGH;
}

bool every(uint32_t *pLast, uint32_t ms) {
  const uint32_t now = millis();
  if ((uint32_t)(now - *pLast) >= ms) {
    *pLast = now;
    return true;
  }
  return false;
}

const char *kindName(uint8_t kind) {
  switch (kind) {
    case EVT_START: return "motion.start";
    case EVT_END:   return "motion.end";
    default:        return "bounce";
  }
}

// Imprime un porcentaje con un decimal: 27.4
void printDuty(uint32_t highMs, uint32_t elapsedMs) {
  if (elapsedMs == 0) { Serial.print(F("0.0")); return; }
  uint32_t d10 = highMs * 1000UL / elapsedMs;
  if (d10 > 1000UL) d10 = 1000UL;
  Serial.print(d10 / 10);
  Serial.print('.');
  Serial.print(d10 % 10);
}

void emitErr(const __FlashStringHelper *code, const char *msg) {
  Serial.print(F("{\"t\":\"err\",\"code\":\""));
  Serial.print(code);
  Serial.print(F("\",\"msg\":\""));
  Serial.print(msg);
  Serial.println(F("\"}"));
}

// ---------------------------------------------------------------------------
// Zumbador pasivo
//
// tone() en el Mega usa el Timer2 y deshabilita el PWM de D9 y D10. En este
// proyecto no se usa PWM, asi que no hay conflicto por tener el zumbador en
// D11 (que es OC1A): el tono lo genera Timer2, no el PWM del pin.
//
// Cada patron es una tabla de tercias (frecuencia, duracion, pausa) en flash.
// Se recorre sin delay(): cada tick comprueba si la fase actual ha terminado.
// ---------------------------------------------------------------------------

const uint16_t BEEP_ARMED[]    PROGMEM = {1800, 110, 70, 1800, 110, 0};
const uint16_t BEEP_DISARMED[] PROGMEM = {650, 350, 0};
const uint16_t BEEP_DETECT[]   PROGMEM = {1500, 70, 60, 1500, 70, 60, 1500, 70, 0};
const uint16_t BEEP_ALARM[]    PROGMEM = {1000, 280, 0, 1300, 280, 0};
const uint16_t BEEP_ERROR[]    PROGMEM = {380, 480, 0};
const uint16_t BEEP_TEST[]     PROGMEM = {880, 110, 50, 1320, 110, 50, 1760, 140, 0};

const uint16_t *g_bsPattern  = nullptr;
uint8_t  g_bsCount    = 0;
uint8_t  g_bsIndex    = 0;
uint8_t  g_bsPhase    = 0;   // 0 = suena, 1 = pausa
uint32_t g_bsPhaseMs  = 0;
uint16_t g_bsPhaseDur = 0;
bool     g_bsActive   = false;
bool     g_bsLoop     = false;
bool     g_bsHasLimit = false;
uint32_t g_bsStartMs  = 0;
uint32_t g_bsLimitMs  = 0;

// Tono continuo de comprobacion: no sigue un patron, suena hasta que vence.
// Existe para verificar el cableado de un vistazo y para buscar la frecuencia
// de resonancia del piezo, que en los modelos baratos anda entre 2 y 4 kHz y
// fuera de ella suena mucho mas flojo.
bool     g_toneActive = false;
uint32_t g_toneUntil  = 0;

// Retardo de salida/entrada: pitidos cada vez mas rapidos.
//
// La cadencia la genera la placa y no el PC a proposito. Asi el aviso no
// depende de que la ventana este libre ni de que el USB vaya fino, que es
// justo lo que no se puede dar por hecho cuando alguien esta saliendo de casa
// o entrando con prisa. El PC solo dice "empieza" y "para"; el resto es local.
//
//   exit : 1000 ms entre pitidos al principio, 250 ms al final
//   entry:  700 ms entre pitidos al principio, 150 ms al final
//
// El tono de entrada es mas agudo que el de salida para distinguirlos de oido
// sin mirar la pantalla, como hacen las centrales de mercado.
bool     g_cdActive  = false;
uint32_t g_cdStartMs = 0;
uint32_t g_cdTotalMs = 0;
uint32_t g_cdNextMs  = 0;
uint16_t g_cdFromMs  = 1000;
uint16_t g_cdToMs    = 250;
uint16_t g_cdToneMs  = 70;
uint16_t g_cdFreq    = 1800;
bool     g_cdToneOn  = false;
uint32_t g_cdToneAt  = 0;

uint32_t cfgSirenMs = 120000UL;

void beeperStop() {
  noTone(PIN_BUZZER);
  digitalWrite(PIN_BUZZER, LOW);
  g_bsActive   = false;
  g_toneActive = false;
  g_cdActive   = false;
  g_cdToneOn   = false;
}

void countdownStart(uint16_t freq, uint32_t totalMs, uint16_t fromMs, uint16_t toMs, bool force) {
  if (!g_buzzerOn || totalMs == 0) return;
  if (!g_warmupDone && !force) return;
  beeperStop();
  g_cdActive  = true;
  g_cdStartMs = millis();
  g_cdTotalMs = totalMs;
  g_cdFromMs  = fromMs;
  g_cdToMs    = toMs;
  g_cdFreq    = freq ? freq : 1800;
  g_cdNextMs  = g_cdStartMs;  // el primer pitido suena ya: confirma la orden
  g_cdToneOn  = false;
}

void countdownTick(uint32_t now) {
  if (!g_cdActive) return;
  if ((uint32_t)(now - g_cdStartMs) >= g_cdTotalMs) {
    beeperStop();
    return;
  }
  if (g_cdToneOn) {
    if ((uint32_t)(now - g_cdToneAt) >= g_cdToneMs) {
      noTone(PIN_BUZZER);
      digitalWrite(PIN_BUZZER, LOW);
      g_cdToneOn = false;
    }
    return;
  }
  if ((int32_t)(now - g_cdNextMs) < 0) return;

  tone(PIN_BUZZER, g_cdFreq);
  g_cdToneOn = true;
  g_cdToneAt = now;

  // Intervalo lineal entre el inicial y el final: se acelera sin saltos y el
  // ultimo tramo es el mas rapido, que es el aviso de "se acaba el plazo".
  const uint32_t done = now - g_cdStartMs;
  const uint32_t span = (uint32_t)(g_cdFromMs - g_cdToMs);
  const uint16_t interval = (uint16_t)(g_cdFromMs - (uint16_t)(span * done / g_cdTotalMs));
  g_cdNextMs = now + (interval > g_cdToneMs ? interval : g_cdToneMs + 20);
}

void beeperEnterPhase() {
  if (!g_bsActive) return;
  const uint16_t *t = g_bsPattern + (uint16_t)g_bsIndex * 3;
  if (g_bsPhase == 0) {
    tone(PIN_BUZZER, pgm_read_word(t));
    g_bsPhaseDur = pgm_read_word(t + 1);
  } else {
    noTone(PIN_BUZZER);
    digitalWrite(PIN_BUZZER, LOW);
    g_bsPhaseDur = pgm_read_word(t + 2);
  }
  g_bsPhaseMs = millis();
}

void beeperStart(const uint16_t *pattern, uint8_t count, bool loop, uint32_t limitMs, bool force) {
  // Durante el calentamiento el zumbador calla: no hay nada que senalizar y
  // se evita un pitido inesperado al enchufar la placa. La prueba manual si
  // suena: es el usuario quien la pide desde la ventana, y sin esto el
  // «probar zumbador» recien conectado parecia un cableado roto.
  if ((!g_warmupDone && !force) || !g_buzzerOn || count == 0) return;
  noTone(PIN_BUZZER);
  g_toneActive = false;
  g_bsPattern  = pattern;
  g_bsCount    = count;
  g_bsIndex    = 0;
  g_bsPhase    = 0;
  g_bsLoop     = loop;
  g_bsActive   = true;
  g_bsStartMs  = millis();
  g_bsLimitMs  = limitMs;
  g_bsHasLimit = limitMs > 0;
  beeperEnterPhase();
}

void beeperTone(uint16_t freq, uint32_t ms, bool force) {
  if (!g_buzzerOn || ms == 0) return;
  if (!g_warmupDone && !force) return;
  noTone(PIN_BUZZER);
  g_bsActive   = false;
  g_toneActive = true;
  g_toneUntil  = millis() + ms;
  tone(PIN_BUZZER, freq ? freq : 2000);
}

void beeperTick(uint32_t now) {
  if (g_cdActive) {
    countdownTick(now);
    return;
  }
  if (g_toneActive) {
    if ((int32_t)(now - g_toneUntil) >= 0) {
      noTone(PIN_BUZZER);
      digitalWrite(PIN_BUZZER, LOW);
      g_toneActive = false;
    }
    return;
  }
  if (!g_bsActive) return;
  if (g_bsHasLimit && (uint32_t)(now - g_bsStartMs) >= g_bsLimitMs) {
    beeperStop();
    return;
  }
  if ((uint32_t)(now - g_bsPhaseMs) < g_bsPhaseDur) return;

  if (g_bsPhase == 0) {
    g_bsPhase = 1;            // termino el tono: toca la pausa
  } else {
    g_bsPhase = 0;            // termino la pausa: siguiente tono
    ++g_bsIndex;
    if (g_bsIndex >= g_bsCount) {
      if (g_bsLoop) {
        g_bsIndex = 0;
      } else {
        beeperStop();
        return;
      }
    }
  }
  beeperEnterPhase();
}

bool beeperByName(const char *name, uint32_t limitMs, uint16_t toneFreq, bool force) {
  // Cortar siempre funciona, aunque el zumbador este apagado: es la orden de
  // "deja de pitar ya" y no puede quedar bloqueada por una comprobacion.
  if (!strcmp(name, "stop")) {
    beeperStop();
    return true;
  }
  // Se comprueba aqui, no solo en beeperStart, para poder contestar `ok:false`
  // cuando la placa esta callada a proposito (zumbador apagado o calentando).
  if (!g_buzzerOn || (!g_warmupDone && !force)) return false;

  // Cualquier patron explicito sustituye a la cuenta atras: el PC acaba de
  // pedir otra cosa y dejar el reloj viejo pitando seria contradecirlo. Se
  // exceptuan las propias cadencias, que se reinician en countdownStart, y el
  // pitido automatico de deteccion (que no pasa por aqui), que debe respetar
  // la cuenta atras de entrada en curso.
  if (strcmp(name, "exit") != 0 && strcmp(name, "entry") != 0) {
    g_cdActive = false;
    g_cdToneOn = false;
  }

  if (!strcmp(name, "tone")) {
    beeperTone(toneFreq, limitMs ? limitMs : 1000UL, force);
  } else if (!strcmp(name, "exit")) {
    // `limitMs` es la duracion total del retardo; la cadencia se acelera sola.
    countdownStart(toneFreq ? toneFreq : 1800, limitMs ? limitMs : 45000UL,
                   1000, 250, force);
  } else if (!strcmp(name, "entry")) {
    countdownStart(toneFreq ? toneFreq : 2200, limitMs ? limitMs : 30000UL,
                   700, 150, force);
  } else if (!strcmp(name, "armed")) {
    beeperStart(BEEP_ARMED, 2, false, limitMs, force);
  } else if (!strcmp(name, "disarmed")) {
    beeperStart(BEEP_DISARMED, 1, false, limitMs, force);
  } else if (!strcmp(name, "detect")) {
    beeperStart(BEEP_DETECT, 3, false, limitMs, force);
  } else if (!strcmp(name, "alarm")) {
    // Una sirena sin limite mantendria el tono hasta el reset. Se acota
    // siempre, aunque el PC olvide enviar ms=.
    beeperStart(BEEP_ALARM, 2, true, limitMs ? limitMs : cfgSirenMs, force);
  } else if (!strcmp(name, "error")) {
    beeperStart(BEEP_ERROR, 1, false, limitMs, force);
  } else if (!strcmp(name, "test")) {
    beeperStart(BEEP_TEST, 3, false, limitMs, force);
  } else {
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Emision
// ---------------------------------------------------------------------------

void emitHello() {
  Serial.print(F("{\"t\":\"hello\",\"proto\":1,\"dev\":\"mega-pir\",\"fw\":\""));
  Serial.print(FW_VERSION);
  Serial.print(F("\",\"boot\":"));
  Serial.print(g_bootMs);
  Serial.print(F(",\"zones\":[{\"id\":1,\"pin\":"));
  Serial.print(PIN_PIR);
  Serial.print(F(",\"name\":\"pir\"}]"));
  Serial.print(F(",\"caps\":[\"hello\",\"set\",\"replay\",\"calibrate\",\"beep\"],\"cfg\":{"));
  Serial.print(F("\"warmup_ms\":"));       Serial.print(cfgWarmupMs);
  Serial.print(F(",\"debounce_ms\":"));    Serial.print(cfgDebounceMs);
  Serial.print(F(",\"hb_ms\":"));          Serial.print(cfgHbMs);
  Serial.print(F(",\"calib_window_ms\":"));Serial.print(cfgCalibWindowMs);
  Serial.print(F(",\"buzzer_pin\":"));     Serial.print(PIN_BUZZER);
  Serial.print(F(",\"buzzer\":"));         Serial.print(g_buzzerOn ? 1 : 0);
  Serial.print(F(",\"armed\":"));          Serial.print(g_armed ? 1 : 0);
  // `wdt_rst` solo puede valer 1 en el hello que sigue a un reinicio del
  // watchdog: es la unica prueba de que la placa se cayo y se levanto sola.
  Serial.print(F(",\"wdt_rst\":"));        Serial.print(g_wdtReset ? 1 : 0);
  Serial.println(F("}}"));
}

void emitEvent(const Event &e) {
  Serial.print(F("{\"t\":\"evt\",\"seq\":"));
  Serial.print(e.seq);
  Serial.print(F(",\"kind\":\""));
  Serial.print(kindName(e.kind));
  Serial.print(F("\",\"z\":1,\"ms\":"));
  Serial.print(e.ms);
  if (e.kind == EVT_END) {
    Serial.print(F(",\"dur\":"));
    Serial.print(e.dur);
  }
  Serial.println(F("}"));
}

void emitHeartbeat() {
  Serial.print(F("{\"t\":\"hb\",\"up\":"));
  Serial.print(millis());
  Serial.print(F(",\"lvl\":"));
  Serial.print(g_stableLevel ? 1 : 0);
  Serial.print(F(",\"raw\":"));
  Serial.print(readRawEdges());
  Serial.print(F(",\"buf\":"));
  Serial.print(evtCount);
  Serial.print(F(",\"lost\":"));
  Serial.print(evtLost);
  Serial.print(F(",\"calib\":"));
  Serial.print(g_calibOn ? 1 : 0);
  Serial.println(F("}"));
}

void emitCalibEdge(uint32_t ms, uint8_t lvl, uint32_t dt) {
  Serial.print(F("{\"t\":\"calib.edge\",\"ms\":"));
  Serial.print(ms);
  Serial.print(F(",\"lvl\":"));
  Serial.print(lvl);
  Serial.print(F(",\"dt\":"));
  Serial.print(dt);
  Serial.println(F("}"));
}

void emitCalibWindow(uint32_t elapsed, uint32_t edges, uint32_t highMs,
                     uint32_t minGap, uint32_t maxGap, uint32_t noise) {
  Serial.print(F("{\"t\":\"calib.window\",\"ms\":"));
  Serial.print(millis());
  Serial.print(F(",\"elapsed_ms\":"));
  Serial.print(elapsed);
  Serial.print(F(",\"edges\":"));
  Serial.print(edges);
  Serial.print(F(",\"high_ms\":"));
  Serial.print(highMs);
  Serial.print(F(",\"duty\":"));
  printDuty(highMs, elapsed);
  Serial.print(F(",\"min_gap\":"));
  Serial.print(minGap);
  Serial.print(F(",\"max_gap\":"));
  Serial.print(maxGap);
  Serial.print(F(",\"bounces\":"));
  Serial.print(noise);
  Serial.println(F("}"));
}

void emitCalibDone() {
  const uint32_t elapsed = millis() - g_calibStartMs;
  Serial.print(F("{\"t\":\"calib.done\",\"elapsed_ms\":"));
  Serial.print(elapsed);
  Serial.print(F(",\"edges\":"));
  Serial.print(tEdges);
  Serial.print(F(",\"high_ms\":"));
  Serial.print(tHigh);
  Serial.print(F(",\"duty\":"));
  printDuty(tHigh, elapsed);
  Serial.print(F(",\"min_gap\":"));
  Serial.print(tMinGap);
  Serial.print(F(",\"max_gap\":"));
  Serial.print(tMaxGap);
  Serial.print(F(",\"gaps\":"));
  Serial.print(tGaps);
  Serial.print(F(",\"bounces\":"));
  Serial.print(noiseSince(tNoiseAt));
  Serial.println(F("}"));
}

void emitAck(uint32_t id, bool ok, const char *op) {
  Serial.print(F("{\"t\":\"ack\",\"id\":"));
  Serial.print(id);
  Serial.print(F(",\"ok\":"));
  Serial.print(ok ? F("true") : F("false"));
  Serial.print(F(",\"op\":\""));
  Serial.print(op);
  Serial.println(F("\"}"));
}

// ---------------------------------------------------------------------------
// Buffer de eventos
// ---------------------------------------------------------------------------

void popEvent() {
  if (evtCount == 0) return;
  evtHead = (uint8_t)((evtHead + 1) % EVT_CAP);
  --evtCount;
}

void pushEvent(uint8_t kind, uint32_t ms, uint16_t dur) {
  if (evtCount >= EVT_CAP) {
    popEvent();
    ++evtLost;   // el PC debera verlo en el latido
  }
  Event &e = evtBuf[(evtHead + evtCount) % EVT_CAP];
  e.seq  = ++evtSeq;
  e.ms   = ms;
  e.dur  = dur;
  e.kind = kind;
  ++evtCount;
  emitEvent(e);
}

void ackEvents(uint32_t seq) {
  while (evtCount > 0 && evtBuf[evtHead].seq <= seq) {
    popEvent();
  }
}

void replayEvents() {
  for (uint8_t i = 0; i < evtCount; ++i) {
    emitEvent(evtBuf[(evtHead + i) % EVT_CAP]);
  }
}

// ---------------------------------------------------------------------------
// Calibracion
// ---------------------------------------------------------------------------

void calibResetWindow() {
  wEdges   = 0;
  wHigh    = 0;
  wMinGap  = 0;
  wMaxGap  = 0;
  wNoiseAt = currentNoise();
  g_calibWinStart = millis();
}

void calibStart(uint32_t windowMs, uint32_t autoEndMs) {
  cfgCalibWindowMs  = clampU32(windowMs, 500UL, 60000UL);
  cfgCalibAutoEndMs = clampU32(autoEndMs, 1000UL, 600000UL);

  g_calibOn       = true;
  g_calibStartMs  = millis();
  g_calibTickMs   = g_calibStartMs;
  g_calibHasRise  = false;
  g_calibRiseMs   = 0;

  tEdges = 0; tHigh = 0; tMinGap = 0; tMaxGap = 0; tGaps = 0;
  tNoiseAt = currentNoise();

  calibResetWindow();
}

void calibStop() {
  if (!g_calibOn) return;
  // Cerrar la ventana en curso y publicar los totales.
  const uint32_t elapsed = millis() - g_calibWinStart;
  if (elapsed > 0) {
    emitCalibWindow(elapsed, wEdges, wHigh, wMinGap, wMaxGap,
                    noiseSince(wNoiseAt));
  }
  emitCalibDone();
  g_calibOn = false;
}

// ---------------------------------------------------------------------------
// Cambio de nivel confirmado
// ---------------------------------------------------------------------------

void applyLevel(bool newLevel, uint32_t at) {
  g_stableLevel = newLevel;
  g_ledState    = newLevel;
  digitalWrite(PIN_LED, newLevel ? HIGH : LOW);
  ++g_accepted;

  // Lectura atomica: g_isrDtMs es uint32_t y la ISR puede interrumpir.
  noInterrupts();
  const uint32_t dt = g_isrDtMs;
  interrupts();

  if (newLevel) {
    g_activityAt = at;
    pushEvent(EVT_START, at, 0);

    // Aviso local inmediato: no depende del USB ni del PC. Solo con la alarma
    // armada, para no pitar en cada paso durante el uso normal.
    if (g_armed) beeperStart(BEEP_DETECT, 3, false, 0, false);

    if (g_calibOn) {
      emitCalibEdge(at, 1, dt);
      ++wEdges;
      ++tEdges;
      if (g_calibHasRise) {
        const uint32_t gap = at - g_calibRiseMs;
        if (wMinGap == 0 || gap < wMinGap) wMinGap = gap;
        if (gap > wMaxGap) wMaxGap = gap;
        if (tMinGap == 0 || gap < tMinGap) tMinGap = gap;
        if (gap > tMaxGap) tMaxGap = gap;
        ++tGaps;
      }
      g_calibHasRise = true;
      g_calibRiseMs  = at;
    }
  } else {
    const uint16_t dur = (uint16_t)(at - g_activityAt);
    pushEvent(EVT_END, at, dur);

    if (g_calibOn) emitCalibEdge(at, 0, dt);
  }
}

// ---------------------------------------------------------------------------
// Comandos
// ---------------------------------------------------------------------------

bool applySet(char **save) {
  char *kv;
  while ((kv = strtok_r(nullptr, " ", save)) != nullptr) {
    char *eq = strchr(kv, '=');
    if (!eq) continue;
    *eq = '\0';
    const char *k = kv;
    const uint32_t v = strtoul(eq + 1, nullptr, 10);

    if (!strcmp(k, "debounce_ms"))        cfgDebounceMs     = clampU32(v, 10UL, 2000UL);
    else if (!strcmp(k, "hb_ms"))         cfgHbMs           = clampU32(v, 500UL, 60000UL);
    else if (!strcmp(k, "warmup_ms"))     cfgWarmupMs       = clampU32(v, 0UL, 300000UL);
    else if (!strcmp(k, "calib_window_ms")) cfgCalibWindowMs = clampU32(v, 500UL, 60000UL);
    else if (!strcmp(k, "siren_ms"))      cfgSirenMs        = clampU32(v, 1000UL, 600000UL);
    else if (!strcmp(k, "armed"))         g_armed           = (v != 0);
    else if (!strcmp(k, "buzzer")) {
      g_buzzerOn = (v != 0);
      if (!g_buzzerOn) beeperStop();
    }
  }
  return true;
}

void handleCommand(char *line) {
  char *save = nullptr;
  char *verb = strtok_r(line, " ", &save);
  if (!verb) return;

  if (!strcmp(verb, "ackevt")) {
    char *s = strtok_r(nullptr, " ", &save);
    if (s) ackEvents(strtoul(s, nullptr, 10));
    return;
  }

  if (strcmp(verb, "cmd") != 0) {
    emitErr(F("badcmd"), "unknown verb");
    return;
  }

  char *idStr = strtok_r(nullptr, " ", &save);
  char *op    = strtok_r(nullptr, " ", &save);
  if (!idStr || !op) {
    emitErr(F("badcmd"), "missing id or op");
    return;
  }

  const uint32_t id = strtoul(idStr, nullptr, 10);
  bool ok = true;

  if (!strcmp(op, "ping")) {
    // nada que hacer: el ack es la respuesta
  }
  else if (!strcmp(op, "hello")) {
    emitHello();
  }
  else if (!strcmp(op, "set")) {
    ok = applySet(&save);
  }
  else if (!strcmp(op, "beep")) {
    const char *name = "test";
    uint32_t limit = 0;
    uint16_t freq  = 0;  // 0 = la frecuencia propia del patron
    bool force     = false;
    char *kv;
    while ((kv = strtok_r(nullptr, " ", &save)) != nullptr) {
      char *eq = strchr(kv, '=');
      if (!eq) continue;
      *eq = '\0';
      if (!strcmp(kv, "name"))      name  = eq + 1;
      else if (!strcmp(kv, "ms"))   limit = strtoul(eq + 1, nullptr, 10);
      // `freq` solo lo usa el tono continuo; `force` salta el silencio del
      // calentamiento para la prueba manual de cableado.
      else if (!strcmp(kv, "freq")) freq  = (uint16_t)constrain(strtoul(eq + 1, nullptr, 10), 100UL, 8000UL);
      else if (!strcmp(kv, "force")) force = strtoul(eq + 1, nullptr, 10) != 0;
    }
    ok = beeperByName(name, limit, freq, force);
  }
  else if (!strcmp(op, "calib.start")) {
    uint32_t win = cfgCalibWindowMs;
    uint32_t aut = cfgCalibAutoEndMs;
    char *kv;
    while ((kv = strtok_r(nullptr, " ", &save)) != nullptr) {
      char *eq = strchr(kv, '=');
      if (!eq) continue;
      *eq = '\0';
      const uint32_t v = strtoul(eq + 1, nullptr, 10);
      if (!strcmp(kv, "window_ms"))       win = v;
      else if (!strcmp(kv, "auto_end_ms")) aut = v;
    }
    calibStart(win, aut);
  }
  else if (!strcmp(op, "calib.stop")) {
    calibStop();
  }
  else if (!strcmp(op, "replay")) {
    replayEvents();
  }
  else if (!strcmp(op, "reset")) {
    g_rawBaseline = readRawEdges();
    g_accepted    = 0;
    g_noiseSent   = 0;
    evtLost       = 0;
  }
  else {
    emitErr(F("badop"), "unknown op");
    ok = false;
  }

  emitAck(id, ok, op);
}

void pollSerial() {
  while (Serial.available() > 0) {
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdLen > 0) {
        cmdBuf[cmdLen] = '\0';
        handleCommand(cmdBuf);
        cmdLen = 0;
      }
    }
    else if (cmdLen < (CMD_CAP - 1)) {
      cmdBuf[cmdLen++] = c;
    }
    else {
      cmdLen = 0;   // descartar hasta el siguiente salto de linea
      emitErr(F("oversize"), "command too long");
    }
  }
}

// ---------------------------------------------------------------------------
// setup / loop
// ---------------------------------------------------------------------------

void setup() {
  // Lo primero de todo, antes de Serial: si el arranque anterior fue por el
  // watchdog, MCUSR trae WDRF puesto y el perro sigue armado. Leerlo, limpiarlo
  // y desactivarlo aqui evita el bucle de reinicios que deja la placa muerta
  // cuando el gestor de arranque tambien se reinicia.
  const uint8_t mcusr = MCUSR;
  MCUSR = 0;
  wdt_disable();
  g_wdtReset = (mcusr & _BV(WDRF)) != 0;

  pinMode(PIN_PIR, INPUT);   // el HC-SR501 tiene salida push-pull
  pinMode(PIN_LED, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  digitalWrite(PIN_BUZZER, LOW);

  Serial.begin(115200);

  g_bootMs      = millis();
  g_tLastHb     = g_bootMs;
  g_tLastWarmup = g_bootMs;
  g_tLastBlink  = g_bootMs;
  g_rawBaseline = readRawEdges();

  attachInterrupt(digitalPinToInterrupt(PIN_PIR), onPirEdge, CHANGE);

  emitHello();

  // Todo listo: a partir de aqui, si loop() deja de dar senal de vida 8 s, la
  // placa se reinicia sola. Se arma al final para que el arranque (que incluye
  // el hello) no cuente contra el plazo.
  wdt_enable(WDTO_8S);
}

void loop() {
  // La unica condicion para no reiniciarse es seguir pasando por aqui. Todas
  // las ramas de loop() vuelven en menos de 500 ms (la del calentamiento
  // tambien, aunque haga `return`), asi que este punto basta como señal de vida.
  wdt_reset();
  pollSerial();

  const uint32_t now = millis();
  beeperTick(now);

  // --- Calentamiento ------------------------------------------------------
  if (!g_warmupDone) {
    if ((uint32_t)(now - g_bootMs) >= cfgWarmupMs) {
      g_warmupDone = true;
      digitalWrite(PIN_LED, LOW);
      g_ledState = false;

      g_stableLevel  = readPir();
      g_changeArmed  = false;
      g_accepted     = 0;
      g_rawBaseline  = readRawEdges();
      g_activityAt   = now;

      Serial.print(F("{\"t\":\"ready\",\"warmup_ms\":"));
      Serial.print(cfgWarmupMs);
      Serial.println(F("}"));

      if (g_stableLevel) {
        // Salida ya alta al terminar el calentamiento: adopcion sin evento.
        digitalWrite(PIN_LED, HIGH);
        g_ledState = true;
      }
      g_tLastHb = now;
    }
    else {
      if (every(&g_tLastBlink, 500UL)) {
        g_ledState = !g_ledState;
        digitalWrite(PIN_LED, g_ledState ? HIGH : LOW);
      }
      if (every(&g_tLastWarmup, 5000UL)) {
        Serial.print(F("{\"t\":\"hb\",\"up\":"));
        Serial.print(now);
        Serial.print(F(",\"lvl\":"));
        Serial.print(readPir() ? 1 : 0);
        Serial.print(F(",\"raw\":"));
        Serial.print(readRawEdges());
        Serial.print(F(",\"buf\":0,\"lost\":"));
        Serial.print(evtLost);
        Serial.println(F(",\"calib\":0,\"warmup\":1}"));
      }
      return;
    }
  }

  // --- Antirrebote por confirmacion temporal ------------------------------
  const bool level = readPir();

  if (level != g_stableLevel) {
    if (!g_changeArmed) {
      g_changeMs    = now;
      g_changeArmed = true;
    }
    else if ((uint32_t)(now - g_changeMs) >= cfgDebounceMs) {
      // El evento se sella con la hora del flanco, no con la de confirmacion.
      applyLevel(level, g_changeMs);
      g_changeArmed = false;
    }
  }
  else {
    g_changeArmed = false;
  }

  // --- Aviso de rebotes ---------------------------------------------------
  // El contador de ruido sube de forma transitoria cuando un flanco llega
  // justo antes de que el antirrebote lo confirme. Se exige que el valor se
  // mantenga 250 ms antes de darlo por real, para no reportar ese transitorio.
  const uint32_t noise = currentNoise();
  if (noise > g_noiseSent) {
    if (noise != g_noiseCand) {
      g_noiseCand   = noise;
      g_noiseCandMs = now;
    }
    else if ((uint32_t)(now - g_noiseCandMs) >= 250UL) {
      g_noiseSent = noise;
      pushEvent(EVT_BOUNCE, now, 0);
    }
  }
  else {
    g_noiseCand = noise;
  }

  // --- Calibracion --------------------------------------------------------
  if (g_calibOn) {
    if (g_stableLevel) {
      wHigh += now - g_calibTickMs;
      tHigh += now - g_calibTickMs;
    }
    g_calibTickMs = now;

    if ((uint32_t)(now - g_calibWinStart) >= cfgCalibWindowMs) {
      const uint32_t elapsed = now - g_calibWinStart;
      emitCalibWindow(elapsed, wEdges, wHigh, wMinGap, wMaxGap,
                      noiseSince(wNoiseAt));
      calibResetWindow();
    }

    if ((uint32_t)(now - g_calibStartMs) >= cfgCalibAutoEndMs) {
      calibStop();
    }
  }
  else {
    g_calibTickMs = now;
  }

  // --- Latido -------------------------------------------------------------
  if (every(&g_tLastHb, cfgHbMs)) {
    emitHeartbeat();
  }
}
