"""Voz por el altavoz del PC con SAPI de Windows.

Sin dependencias: se usa `System.Speech` de .NET a traves de PowerShell, que
viene con Windows, y `winsound` para reproducir, que viene con CPython. El
precio es que la sintesis arranca un proceso; por eso se sintetiza una sola vez
y el WAV queda en cache.

El servicio nunca bloquea a quien llama:

- `say_event()` programa un aviso y vuelve.
- Un hilo propio espera al vencimiento, sintetiza (si hace falta) y reproduce.
- La cola es acotada: si se llena, se descarta el mensaje mas antiguo. Un
  aviso de hace diez segundos ya no interesa y bloquear la interfaz es peor.

Cada aviso puede llevar su propio retardo y sus propias repeticiones, porque
"presencia detectada" y "alarma desarmada" no piden la misma insistencia.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config import TtsConfig
from ..paths import storage_root

log = logging.getLogger(__name__)

#: Los WAV sintetizados viven en la raiz de datos: junto al `.exe` si se puede
#: escribir alli, o en `%LOCALAPPDATA%` si el ejecutable esta en una carpeta
#: protegida. Se calcula al usarlo, no al importar, para que las pruebas puedan
#: apuntar a otra carpeta con `cache_dir`.
CACHE_DIR = Path("data") / "tts"
SYNTH_TIMEOUT_S = 30.0

# Voces SAPI ya consultadas. `None` = todavia no se ha preguntado a Windows.
_voices_cache: list[str] | None = None


class SpeechError(RuntimeError):
    """No se pudo sintetizar o reproducir."""


class Player(Protocol):
    """Salida de audio. Se inyecta para poder probar sin sonido."""

    def play(self, path: Path) -> None: ...

    def stop(self) -> None: ...


@dataclass(order=True, slots=True)
class SpeechRequest:
    """Aviso programado.

    El orden depende de `due` para que un mensaje con retardo no adelante a uno
    inmediato. El resto de campos no participa en la comparacion.

    ``gap`` es la pausa entre una repeticion y la siguiente; se guarda en
    segundos porque `_sleep()` trabaja con segundos.
    """

    due: float
    key: str = field(compare=False)
    text: str = field(compare=False)
    repeat: int = field(compare=False, default=1)
    gap: float = field(compare=False, default=0.0)
    sequence: int = field(compare=False, default=0)
    cancelled: bool = field(compare=False, default=False)


def _ps_quote(value: str) -> str:
    """Literal de cadena de PowerShell: comillas simples duplicadas."""
    return "'" + value.replace("'", "''") + "'"


def build_sapi_script(text: str, path: Path, *, voice: str, rate: int, volume: int) -> str:
    """Guion de PowerShell que sintetiza `text` en un WAV.

    Se expone para poder comprobarlo en pruebas: el escapado es la parte facil
    de romper y la que peor se diagnostica a distancia.
    """
    lines = [
        "Add-Type -AssemblyName System.Speech",
        "$ErrorActionPreference = 'Stop'",
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
    ]
    if voice:
        # Una voz inexistente no debe impedir el aviso: se usa la del sistema.
        lines.append(f"try {{ $s.SelectVoice({_ps_quote(voice)}) }} catch {{ }}")
    lines.extend(
        [
            f"$s.Rate = {int(rate)}",
            f"$s.Volume = {int(volume)}",
            f"$s.SetOutputToWaveFile({_ps_quote(str(path))})",
            f"$s.Speak({_ps_quote(text)})",
            "$s.Dispose()",
        ]
    )
    return "; ".join(lines)


def synthesize_sapi(
    text: str, path: Path, *, voice: str = "", rate: int = 0, volume: int = 90
) -> None:
    """Sintetiza con SAPI escribiendo un WAV en `path`."""
    script = build_sapi_script(text, path, voice=voice, rate=rate, volume=volume)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(  # noqa: S603 - argumentos fijos, sin shell
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            timeout=SYNTH_TIMEOUT_S,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpeechError(f"no se pudo ejecutar PowerShell: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip() or "sin detalle"
        raise SpeechError(f"SAPI fallo ({completed.returncode}): {detail[:200]}")
    if not path.is_file() or path.stat().st_size == 0:
        raise SpeechError("SAPI no genero el fichero de audio")


def installed_voices() -> list[str]:
    """Voces SAPI instaladas en el equipo. Lista vacia si no se puede consultar.

    Se usa en el dialogo de ajustes; un fallo aqui solo deja el desplegable
    vacio, nunca impide abrir la ventana.

    El resultado se memoriza: la consulta arranca PowerShell (hasta 15 s en un
    equipo lento) y abrir el dialogo dos veces no debe pagar ese coste otra vez
    ni dar la sensacion de que la ventana se ha colgado. Las voces de Windows no
    cambian sin reiniciar el proceso, asi que la cache no se queda obsoleta.
    """
    global _voices_cache
    if _voices_cache is not None:
        return list(_voices_cache)

    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }"
    )
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(  # noqa: S603 - argumentos fijos, sin shell
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            timeout=15.0,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        # No se memoriza el fallo: puede ser transitorio y conviene reintentar
        # la proxima vez que se abra el dialogo.
        return []
    if completed.returncode != 0:
        return []
    text = completed.stdout.decode("utf-8", "replace")
    _voices_cache = [line.strip() for line in text.splitlines() if line.strip()]
    return list(_voices_cache)


class WinsoundPlayer:
    """Reproduccion con `winsound`. `play` bloquea hasta el final del WAV."""

    def play(self, path: Path) -> None:
        try:
            import winsound
        except ImportError as exc:  # pragma: no cover - solo otros sistemas
            raise SpeechError("winsound no esta disponible") from exc
        try:
            # SND_SYNC es 0: sin SND_ASYNC, PlaySound bloquea hasta el final.
            winsound.PlaySound(str(path), winsound.SND_FILENAME)
        except RuntimeError as exc:  # pragma: no cover - sin tarjeta o sin permisos
            raise SpeechError(f"no se pudo reproducir: {exc}") from exc

    def stop(self) -> None:
        try:
            import winsound
        except ImportError:  # pragma: no cover
            return
        # Nada sonando no es un error.
        with suppress(RuntimeError):
            winsound.PlaySound(None, winsound.SND_PURGE)


class SpeechService:
    """Cola acotada de mensajes hablados con retardo y repeticiones."""

    def __init__(
        self,
        config: TtsConfig,
        *,
        cache_dir: Path | None = None,
        synthesizer: Callable[[str, Path], None] | None = None,
        player: Player | None = None,
        max_queue: int = 6,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._enabled = config.enabled
        self._cache_dir = storage_root() / CACHE_DIR if cache_dir is None else Path(cache_dir)
        self._synthesize = synthesizer or self._default_synthesize
        self._player: Player = player if player is not None else WinsoundPlayer()
        self._queue: queue.Queue[SpeechRequest] = queue.Queue(maxsize=max(1, max_queue))
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._counter = itertools.count(1)
        self._pending: dict[int, SpeechRequest] = {}

        self.last_error = ""
        self.dropped = 0
        self.spoken = 0
        self._last_text = ""

    # -- API ---------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def last_text(self) -> str:
        return self._last_text

    @property
    def pending(self) -> int:
        """Avisos programados que aun no han sonado."""
        return len(self._pending)

    def set_enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        if not self._enabled:
            self.stop()

    def reconfigure(self, config: TtsConfig) -> None:
        """Aplica una configuracion recargada sin recrear el hilo."""
        self._config = config
        self._enabled = config.enabled

    def say(self, text: str) -> bool:
        """Encola un mensaje sin retardo. Devuelve False si no se puede."""
        return self.say_event("generic", text)

    def say_event(
        self,
        key: str,
        text: str,
        *,
        repeat: int | None = None,
        delay_ms: int | None = None,
        gap_ms: int | None = None,
    ) -> bool:
        """Programa un aviso hablado.

        `repeat`, `delay_ms` y `gap_ms` toman por defecto los del evento
        configurado. Un texto vacio o un evento desactivado no encolan nada.
        """
        message = text.strip()
        if not self._enabled or not message:
            return False

        event = self._config.event(key)
        if not event.enabled:
            log.debug("aviso de voz '%s' desactivado por configuracion", key)
            return False

        repeats = event.repeat if repeat is None else int(repeat)
        delay = event.delay_ms if delay_ms is None else int(delay_ms)
        gap = event.gap_ms if gap_ms is None else int(gap_ms)
        if repeats <= 0:
            return False

        self._ensure_thread()
        request = SpeechRequest(
            due=self._clock() + max(0, delay) / 1000.0,
            key=key,
            text=message,
            repeat=max(1, repeats),
            gap=max(0, gap) / 1000.0,
            sequence=next(self._counter),
        )

        with self._lock:
            if self._queue.full():
                with suppress(queue.Empty):
                    dropped = self._queue.get_nowait()
                    self._pending.pop(dropped.sequence, None)
                self.dropped += 1
            self._queue.put_nowait(request)
            self._pending[request.sequence] = request
        return True

    def cancel_pending(self, keys: str | tuple[str, ...] | None = None) -> int:
        """Anula avisos programados que todavia no han sonado.

        Se usa al cambiar de estado: no tiene sentido oir "presencia detectada"
        tres segundos despues de haber desarmado. Devuelve cuantos se anularon.
        """
        targets = None if keys is None else ({keys} if isinstance(keys, str) else set(keys))
        cancelled = 0
        with self._lock:
            for request in self._pending.values():
                if targets is None or request.key in targets:
                    request.cancelled = True
                    cancelled += 1
            if targets is None:
                self._pending.clear()
            else:
                for sequence in [seq for seq, req in self._pending.items() if req.key in targets]:
                    self._pending.pop(sequence, None)
        if cancelled:
            log.debug("anulados %d avisos de voz pendientes", cancelled)
        return cancelled

    def stop(self) -> None:
        """Corta la reproduccion, vacia la cola y anula lo programado."""
        self._player.stop()
        self.cancel_pending()
        with self._lock:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def close(self) -> None:
        self._stop.set()
        self.stop()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    def synthesize(self, text: str) -> Path:
        """Garantiza el WAV en cache y devuelve su ruta. Bloquea."""
        path = self._cache_path(text)
        if not path.is_file():
            self._synthesize_cached(text, path)
        return path

    def play_file(self, path: Path) -> None:
        """Reproduce un WAV ya sintetizado, una vez."""
        self._player.play(path)

    def stop_audio(self) -> None:
        self._player.stop()

    # -- Interno -----------------------------------------------------------

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="speech", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                request = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if request.cancelled:
                continue

            wait = request.due - self._clock()
            if wait > 0 and self._sleep(wait, request):
                continue

            try:
                self._speak(request)
            except Exception as exc:  # noqa: BLE001 - un fallo de audio no tumba el hilo
                self.last_error = str(exc)
                log.warning("aviso de voz fallido: %s", exc)
            finally:
                with self._lock:
                    self._pending.pop(request.sequence, None)

    def _sleep(self, seconds: float, request: SpeechRequest) -> bool:
        """Espera al vencimiento. Devuelve True si el aviso se cancelo."""
        deadline = self._clock() + seconds
        while not self._stop.is_set():
            if request.cancelled:
                return True
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
        return True

    def _speak(self, request: SpeechRequest) -> None:
        path = self.synthesize(request.text)
        self._last_text = request.text
        for index in range(request.repeat):
            if self._stop.is_set() or request.cancelled:
                return
            # La pausa va entre repeticiones, no antes de la primera: el
            # retardo inicial ya lo ha esperado `_run`. Se usa `_sleep` para
            # que `cancel_pending()` corte tambien durante el intervalo.
            if index and request.gap and self._sleep(request.gap, request):
                return
            self._player.play(path)
        self.spoken += 1
        self.last_error = ""

    def _synthesize_cached(self, text: str, path: Path) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        # Se escribe a un temporal y se renombra: un WAV a medias no debe
        # quedar en cache y reproducirse en el siguiente aviso.
        temporary = path.with_suffix(".part")
        try:
            self._synthesize(text, temporary)
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

    def _cache_path(self, text: str) -> Path:
        key = "\x1f".join(
            (
                text,
                self._config.voice,
                str(self._config.rate),
                str(self._config.volume),
            )
        )
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self._cache_dir / f"{digest}.wav"

    def _default_synthesize(self, text: str, path: Path) -> None:
        synthesize_sapi(
            text,
            path,
            voice=self._config.voice,
            rate=self._config.rate,
            volume=self._config.volume,
        )


def describe_voice(config: TtsConfig, key: str) -> str:
    """Resumen legible de un evento de voz, para tooltips y registro."""
    event = config.event(key)
    parts: list[Any] = [f"texto={event.text!r}"]
    parts.append(f"repeticiones={event.repeat}")
    if event.gap_ms:
        parts.append(f"intervalo={event.gap_ms / 1000:.1f} s")
    if event.delay_ms:
        parts.append(f"retardo={event.delay_ms / 1000:.1f} s")
    if not event.log:
        parts.append("sin registro")
    if not event.enabled:
        parts.append("desactivado")
    return " · ".join(str(p) for p in parts)
