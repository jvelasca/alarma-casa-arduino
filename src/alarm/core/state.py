"""Estado de la alarma, retardos, rearme automatico y limites de aviso.

La maquina tiene seis estados. Los dos de en medio son los retardos de una
alarma profesional: al armar no se vigila de inmediato (hay que salir) y al
detectar no se dispara de inmediato (hay que llegar al teclado y desarmar).
Sin ellos, entrar por la puerta y que suene la sirena es el mismo instante, y
eso hace la alarma inservible en el uso diario.

    DISARMED --arm()--> ARMING --tick()--> ARMED
    ARMED --motion--> PENDING --tick()--> TRIGGERED
    TRIGGERED --fin de sirena--> REARMING --tick()--> ARMED
    cualquiera --disarm()--> DISARMED

`REARMING` es el rearme automatico tras un disparo: cuando la sirena termina,
la alarma espera y vuelve a vigilar sola, hasta un numero maximo de veces. Es
opcional; con `auto_rearm=False` la maquina se comporta como antes.

El controlador es puro: no toca el dispositivo, ni la red, ni la interfaz. Cada
metodo devuelve la transicion ocurrida (o None) y quien llama decide los
efectos. Asi se puede probar sin hilos ni hardware; el reloj se inyecta.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..protocol.messages import MotionEvent, MotionKind

log = logging.getLogger(__name__)


class AlarmState(StrEnum):
    DISARMED = "disarmed"
    ARMING = "arming"  # retardo de salida: aun no vigila
    ARMED = "armed"
    PENDING = "pending"  # retardo de entrada: detectado, aun sin disparar
    TRIGGERED = "triggered"
    REARMING = "rearming"  # tras un disparo: esperando para volver a armar


# Estados en los que corre una cuenta atras y el hero debe pintar el reloj.
DELAY_STATES = (AlarmState.ARMING, AlarmState.PENDING, AlarmState.REARMING)

# Acciones que un consumidor debe traducir a efectos. Son cadenas y no un enum
# para que el registro y la configuracion puedan hablar el mismo idioma.
ACTION_ARMED = "armed"
ACTION_ARMING = "arming"
ACTION_PENDING = "pending"
ACTION_DISARMED = "disarmed"
ACTION_TRIGGERED = "triggered"
ACTION_SILENCED = "silenced"
ACTION_REARMING = "rearming"
ACTION_REARMED = "rearmed"
ACTION_REARM_EXHAUSTED = "rearm_exhausted"

#: Version del formato del snapshot. Se guarda con el estado para poder
#: descartar uno antiguo sin romper el arranque.
SNAPSHOT_VERSION = 1


@dataclass(frozen=True, slots=True)
class Transition:
    previous: AlarmState
    current: AlarmState
    action: str

    @property
    def state_changed(self) -> bool:
        return self.previous is not self.current


@dataclass(frozen=True, slots=True)
class ControllerSnapshot:
    """Estado interno del controlador en un instante, para poder deshacerlo.

    Es un punto de control en memoria, no el snapshot que se guarda en disco:
    conserva los plazos tal cual estan (reloj monotono incluido) y los limites
    de aviso ya consumidos. Se usa cuando un evento se calculo pero no se pudo
    persistir: la maquina vuelve exactamente a donde estaba y el reenvio de la
    placa produce la misma transicion, con sus efectos una sola vez.
    """

    state: AlarmState
    silenced: bool
    deadline: float | None
    delay_total_s: float
    siren_deadline: float | None
    rearm_count: int
    last_cause: tuple[tuple[str, float], ...]
    sent: tuple[float, ...]


class AlarmController:
    """Maquina de estados con retardos de entrada y salida.

    Los retardos se expresan en segundos y 0 los desactiva: con 0 el
    comportamiento es el de antes (armar vigila al instante y detectar dispara
    al instante), que es lo que esperan las pruebas y quien no los quiera.

    `siren_s` es lo que dura la sirena en la placa. El controlador no la hace
    sonar, pero necesita saber cuando calla para empezar a contar la espera del
    rearme automatico.
    """

    def __init__(
        self,
        *,
        armed_on_start: bool = False,
        cooldown_s: int = 180,
        max_per_hour: int = 6,
        exit_delay_s: int = 0,
        entry_delay_s: int = 0,
        auto_rearm: bool = False,
        rearm_delay_s: int = 300,
        max_rearms: int = 3,
        siren_s: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._state = AlarmState.ARMED if armed_on_start else AlarmState.DISARMED
        self._silenced = False
        self._cooldown_s = max(0, int(cooldown_s))
        self._max_per_hour = max(1, int(max_per_hour))
        self._exit_delay_s = max(0, int(exit_delay_s))
        self._entry_delay_s = max(0, int(entry_delay_s))
        # Instante (en el reloj inyectado) en que vence el retardo en curso.
        self._deadline: float | None = None
        # Duracion con la que se abrio ese retardo. La guarda el controlador y
        # no se lee de la configuracion porque esta puede cambiar a mitad de la
        # cuenta atras y el reloj grande no debe saltar a otro total.
        self._delay_total_s = 0.0
        # Rearme automatico tras un disparo.
        self._auto_rearm = bool(auto_rearm)
        self._rearm_delay_s = max(0, int(rearm_delay_s))
        self._max_rearms = max(1, int(max_rearms))
        self._rearm_count = 0
        self._siren_s = max(0.0, float(siren_s))
        # Instante en que calla la sirena. Es independiente de `_deadline`
        # porque el disparo no es un retardo que se pinte en el reloj grande.
        self._siren_deadline: float | None = None
        self._last_cause: dict[str, float] = {}
        self._sent: deque[float] = deque()

    # -- Consulta ----------------------------------------------------------

    @property
    def state(self) -> AlarmState:
        return self._state

    @property
    def silenced(self) -> bool:
        return self._silenced

    @property
    def armed(self) -> bool:
        """El dispositivo debe pitar al detectar.

        Durante el retardo de salida todavia no: se esta saliendo de casa y el
        PIR vera a la persona que se va. Si la placa pitara, el propio armado
        sonaria a intrusion.
        """
        return self._state not in (AlarmState.DISARMED, AlarmState.ARMING)

    @property
    def delay_kind(self) -> str | None:
        """`exit`, `entry`, `rearm` o None segun la cuenta atras en curso."""
        if self._state is AlarmState.ARMING:
            return "exit"
        if self._state is AlarmState.PENDING:
            return "entry"
        if self._state is AlarmState.REARMING:
            return "rearm"
        return None

    @property
    def rearm_enabled(self) -> bool:
        return self._auto_rearm

    @property
    def rearm_count(self) -> int:
        """Rearmes automaticos consumidos desde el ultimo armado manual."""
        return self._rearm_count

    @property
    def rearm_max(self) -> int:
        return self._max_rearms

    @property
    def rearm_exhausted(self) -> bool:
        """Se agotaron los rearmes: queda disparada hasta que alguien desarme."""
        return (
            self._auto_rearm
            and self._state is AlarmState.TRIGGERED
            and self._rearm_count >= self._max_rearms
            and self._siren_deadline is None
        )

    def siren_remaining_s(self, now: float | None = None) -> float | None:
        """Lo que le queda a la sirena del disparo, o None si no hay ninguna."""
        if self._siren_deadline is None:
            return None
        moment = self._clock() if now is None else now
        return max(0.0, self._siren_deadline - moment)

    def remaining_s(self, now: float | None = None) -> float | None:
        """Segundos que faltan para que venza el retardo, o None si no hay."""
        if self._deadline is None:
            return None
        moment = self._clock() if now is None else now
        return max(0.0, self._deadline - moment)

    @property
    def delay_total_s(self) -> float:
        """Duracion completa del retardo en curso, o 0 si no hay ninguno.

        Sirve para dibujar el avance en el reloj grande. Se conserva durante
        todo el retardo aunque `set_delays` cambie la configuracion: el plazo
        en curso no se recorta a mitad (ver `set_delays`).
        """
        return self._delay_total_s if self._deadline is not None else 0.0

    # -- Ordenes del usuario ----------------------------------------------

    def arm(self) -> Transition | None:
        if self._state is not AlarmState.DISARMED:
            return None
        # Armar a mano empieza una sesion nueva: los rearmes automaticos se
        # cuentan desde aqui, no desde el ultimo disparo.
        self._rearm_count = 0
        if self._exit_delay_s:
            self._start_delay(self._exit_delay_s)
            return self._move(AlarmState.ARMING, ACTION_ARMING)
        return self._move(AlarmState.ARMED, ACTION_ARMED)

    def disarm(self) -> Transition | None:
        if self._state is AlarmState.DISARMED:
            return None
        # Desarmar cancela el retardo en curso: es justo la salida que ofrece
        # una alarma profesional durante la cuenta atras de entrada. Tambien
        # deja a cero el contador de rearmes y cualquier espera pendiente.
        self._deadline = None
        self._delay_total_s = 0.0
        self._siren_deadline = None
        self._rearm_count = 0
        return self._move(AlarmState.DISARMED, ACTION_DISARMED)

    def silence(self) -> Transition | None:
        """Detiene los avisos sin desarmar. Idempotente."""
        if self._state is not AlarmState.TRIGGERED or self._silenced:
            return None
        self._silenced = True
        # La espera del rearme cuenta desde que calla la sirena, no desde el
        # disparo: silenciar a mano no debe retrasar el rearme.
        if self._auto_rearm and self._rearm_count < self._max_rearms:
            self._siren_deadline = self._clock()
        return Transition(self._state, self._state, ACTION_SILENCED)

    def panic(self) -> Transition | None:
        """Dispara a mano, sin esperar al sensor (boton de panico).

        Se permite desde cualquier estado, tambien desde `DISARMED`: es el
        boton que se pulsa cuando ya hay alguien dentro. Durante el retardo de
        salida lo cancela, porque la sirena manda sobre la cuenta atras.
        """
        if self._state is AlarmState.TRIGGERED:
            return None
        self._rearm_count = 0
        return self._move(AlarmState.TRIGGERED, ACTION_TRIGGERED)

    def restart_siren(self, seconds: float | None = None) -> None:
        """La sirena vuelve a sonar: reinicia su plazo.

        Se llama cuando la placa se reinicia con la alarma disparada y hay que
        reenviarle el patron: si el rearme contara desde el disparo original, el
        silencio posterior al reinicio no se tendria en cuenta.
        """
        if self._state is not AlarmState.TRIGGERED or self._silenced:
            return
        if self._rearm_count >= self._max_rearms:
            return
        duration = self._siren_s if seconds is None else max(0.0, float(seconds))
        self._siren_deadline = self._clock() + duration

    # -- Eventos y reloj ---------------------------------------------------

    def feed(self, event: MotionEvent) -> Transition | None:
        """Procesa un evento. Solo `motion.start` con la alarma armada dispara.

        Con retardo de entrada, la primera deteccion abre la cuenta atras y las
        siguientes no la reinician: quien ya esta dentro no debe poder alargar
        el plazo paseandose delante del sensor.

        `motion.end` no significa que la persona se haya ido: es el fin del
        pulso de retencion del modulo (ver doc/02-PROTOCOLO.md).
        """
        if event.kind is not MotionKind.START:
            return None
        if self._state is not AlarmState.ARMED:
            return None
        if self._entry_delay_s:
            self._start_delay(self._entry_delay_s)
            return self._move(AlarmState.PENDING, ACTION_PENDING)
        return self._move(AlarmState.TRIGGERED, ACTION_TRIGGERED)

    def tick(self, now: float | None = None) -> Transition | None:
        """Avanza el reloj de los retardos y del rearme. Devuelve lo que venza.

        La interfaz lo llama varias veces por segundo; con reloj inyectado las
        pruebas lo llaman con el instante que quieran, sin esperar de verdad.
        """
        moment = self._clock() if now is None else now
        if self._deadline is None:
            # Sin retardo, lo unico que puede vencer es la sirena del disparo.
            return self._siren_expired(moment)
        if moment < self._deadline:
            return None
        self._deadline = None
        self._delay_total_s = 0.0
        if self._state is AlarmState.ARMING:
            return self._move(AlarmState.ARMED, ACTION_ARMED)
        if self._state is AlarmState.PENDING:
            return self._move(AlarmState.TRIGGERED, ACTION_TRIGGERED)
        if self._state is AlarmState.REARMING:
            return self._rearmed()
        return None

    def _siren_expired(self, moment: float) -> Transition | None:
        """Decide que hacer cuando la sirena del disparo ya deberia haber callado."""
        if not self._auto_rearm or self._siren_deadline is None:
            return None
        if moment < self._siren_deadline:
            return None
        # Se consume el plazo en cualquier caso: sea rearme o agotamiento, no
        # puede volver a dispararse este aviso en cada tic.
        self._siren_deadline = None
        if self._rearm_count >= self._max_rearms:
            return Transition(self._state, self._state, ACTION_REARM_EXHAUSTED)
        if self._rearm_delay_s <= 0:
            # Espera cero: rearma en el mismo instante en que calla la sirena.
            return self._rearmed()
        self._start_delay(self._rearm_delay_s)
        return self._move(AlarmState.REARMING, ACTION_REARMING)

    def _rearmed(self) -> Transition:
        """Vuelve a vigilar tras un disparo, sin retardo de salida.

        El retardo de salida existe para que a nadie le pille el PIR saliendo de
        casa; aqui la persona (o el intruso) ya esta dentro, asi que volver a
        vigilar al instante es justo lo que se busca.
        """
        self._rearm_count += 1
        return self._move(AlarmState.ARMED, ACTION_REARMED)

    # -- Limites de aviso --------------------------------------------------

    def allow(self, cause: str, *, now: float | None = None) -> bool:
        """Decide si un aviso de `cause` puede salir ahora.

        Aplica dos limites independientes: enfriamiento por causa y tope total
        por hora. Si devuelve True, el aviso queda contabilizado.
        """
        moment = self._clock() if now is None else now

        last = self._last_cause.get(cause)
        if last is not None and moment - last < self._cooldown_s:
            return False

        while self._sent and moment - self._sent[0] >= 3600.0:
            self._sent.popleft()
        if len(self._sent) >= self._max_per_hour:
            return False

        self._last_cause[cause] = moment
        self._sent.append(moment)
        return True

    def reset_limits(self) -> None:
        self._last_cause.clear()
        self._sent.clear()

    def set_limits(self, *, cooldown_s: int, max_per_hour: int) -> None:
        self._cooldown_s = max(0, int(cooldown_s))
        self._max_per_hour = max(1, int(max_per_hour))

    def set_delays(self, *, exit_delay_s: int, entry_delay_s: int) -> None:
        """Cambia los retardos para la proxima vez.

        Un retardo ya en curso conserva su plazo: recortarlo a mitad haria que
        la alarma se armara o disparara de golpe sin que nadie lo espere.
        """
        self._exit_delay_s = max(0, int(exit_delay_s))
        self._entry_delay_s = max(0, int(entry_delay_s))

    def set_rearm(self, *, auto_rearm: bool, rearm_delay_s: int, max_rearms: int) -> None:
        """Cambia la politica de rearme automatico para la proxima vez.

        La espera en curso no se recorta, igual que los retardos: el plazo se
        abrio con un valor y cambiarlo a mitad daria un salto en el reloj.
        """
        self._auto_rearm = bool(auto_rearm)
        self._rearm_delay_s = max(0, int(rearm_delay_s))
        self._max_rearms = max(1, int(max_rearms))

    def set_siren_s(self, seconds: float) -> None:
        """Duracion de la sirena para el proximo disparo.

        Si ahora mismo esta sonando, su plazo no se toca: el aviso en curso es
        el que se prometio al disparar.
        """
        self._siren_s = max(0.0, float(seconds))

    # -- Punto de control y recuperacion -----------------------------------

    def capture(self) -> ControllerSnapshot:
        """Copia el estado interno para poder volver a el con ``restore``.

        Incluye los plazos y los contadores de aviso: si el evento que los
        movio no se pudo guardar, dejarlos avanzados haria que el reenvio no
        encontrara nada que contar y la sirena no sonara.
        """
        return ControllerSnapshot(
            state=self._state,
            silenced=self._silenced,
            deadline=self._deadline,
            delay_total_s=self._delay_total_s,
            siren_deadline=self._siren_deadline,
            rearm_count=self._rearm_count,
            last_cause=tuple(self._last_cause.items()),
            sent=tuple(self._sent),
        )

    def restore(self, snapshot: ControllerSnapshot) -> None:
        """Devuelve el controlador al instante capturado por ``capture``."""
        self._state = snapshot.state
        self._silenced = snapshot.silenced
        self._deadline = snapshot.deadline
        self._delay_total_s = snapshot.delay_total_s
        self._siren_deadline = snapshot.siren_deadline
        self._rearm_count = snapshot.rearm_count
        self._last_cause = dict(snapshot.last_cause)
        self._sent = deque(snapshot.sent)

    def to_snapshot(self, now: float | None = None) -> dict[str, object]:
        """Estado completo para poder retomarlo tras un cierre.

        No basta con recordar «armada»: si el cierre pilla la alarma en el
        retardo de entrada o con la sirena sonando, al arrancar hay que saber
        cuanto quedaba. Se guarda el tiempo restante y no el instante de
        vencimiento porque el reloj del controlador es monotono y empieza de
        cero en cada proceso; quien recupera descuenta lo que ha pasado fuera.
        """
        moment = self._clock() if now is None else now
        return {
            "v": SNAPSHOT_VERSION,
            "state": self._state.value,
            "saved_at": time.time(),
            "delay_remaining_s": self.remaining_s(moment),
            "delay_total_s": self._delay_total_s,
            "siren_remaining_s": self.siren_remaining_s(moment),
            "silenced": self._silenced,
            "rearm_count": self._rearm_count,
        }

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, object],
        *,
        elapsed_s: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        **options: Any,
    ) -> AlarmController:
        """Reconstruye el controlador desde ``to_snapshot``.

        ``elapsed_s`` es el tiempo que la aplicacion estuvo cerrada: se descuenta
        de los plazos, de modo que un retardo de entrada que vencio mientras el
        PC estaba apagado no se reabre al arrancar. Los plazos vencidos quedan
        con vencimiento inmediato: la primera llamada a ``tick()`` devuelve la
        transicion que toca y el llamador decide los efectos.
        """
        raw_state = str(snapshot.get("state", "")).strip().lower()
        try:
            state = AlarmState(raw_state)
        except ValueError:
            log.warning("estado guardado ilegible (%r): se arranca desarmada", raw_state)
            state = AlarmState.DISARMED

        controller = cls(
            armed_on_start=state is not AlarmState.DISARMED,
            clock=clock,
            **options,
        )
        controller._state = state
        controller._silenced = state is AlarmState.TRIGGERED and bool(
            snapshot.get("silenced", False)
        )
        controller._rearm_count = _as_count(snapshot.get("rearm_count"))

        now = clock()
        downtime = max(0.0, float(elapsed_s))

        delay_left = _as_seconds(snapshot.get("delay_remaining_s"))
        if state in DELAY_STATES and delay_left is not None:
            remaining = max(0.0, delay_left - downtime)
            controller._deadline = now + remaining
            total = _as_seconds(snapshot.get("delay_total_s")) or 0.0
            # El total nunca por debajo de lo que queda: el reloj grande del
            # hero no debe empezar con mas avanzado que el final.
            controller._delay_total_s = max(total, remaining, 1.0)
        else:
            controller._deadline = None
            controller._delay_total_s = 0.0

        siren_left = _as_seconds(snapshot.get("siren_remaining_s"))
        if state is AlarmState.TRIGGERED and siren_left is not None:
            controller._siren_deadline = now + max(0.0, siren_left - downtime)
        else:
            controller._siren_deadline = None

        return controller

    # -- Interno -----------------------------------------------------------

    def _start_delay(self, seconds: int) -> None:
        self._deadline = self._clock() + float(seconds)
        self._delay_total_s = float(seconds)

    def _move(self, new_state: AlarmState, action: str) -> Transition:
        previous = self._state
        self._state = new_state
        self._silenced = False
        if new_state not in DELAY_STATES:
            self._deadline = None
            self._delay_total_s = 0.0
        # La sirena solo cuenta en el disparo. Al entrar se arranca su plazo y
        # al salir se olvida: un disparo viejo no puede rearmar la alarma.
        if new_state is AlarmState.TRIGGERED:
            self._siren_deadline = self._clock() + self._siren_s
        elif new_state is not AlarmState.REARMING:
            self._siren_deadline = None
        return Transition(previous, new_state, action)


# ---------------------------------------------------------------------------
# Ayudas de deserializacion
# ---------------------------------------------------------------------------


def _as_seconds(value: object) -> float | None:
    """Convierte un valor del snapshot en segundos, o ``None`` si no lo es."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value))
        except ValueError:
            return None
    return None


def _as_count(value: object) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0
