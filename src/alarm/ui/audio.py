"""Avisos sonoros del modo de ajuste.

Se centralizan aqui para que la consola y la ventana suenen igual y haya un
unico interruptor. En Windows usa `winsound.Beep`, que genera el tono con el
temporizador del sistema y no depende de que el mezclador este libre.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_ENABLED = False

# (frecuencia en Hz, duracion en ms). Secuencias cortas a proposito: el usuario
# esta lejos del teclado y necesita distinguirlas de oido.
_SEQUENCES: dict[str, tuple[tuple[int, int], ...]] = {
    # Sal de la estancia: un tono grave y largo.
    "leave": ((660, 450),),
    # Empieza la fase de reposo: dos graves cortos.
    "quiet": ((600, 250), (600, 250)),
    # Empieza la fase de movimiento: tres agudos rapidos.
    "move": ((1050, 130), (1050, 130), (1050, 130)),
    # Fin de la prueba: un tono descendente.
    "done": ((880, 220), (660, 220), (520, 400)),
}


def set_enabled(value: bool) -> None:
    global _ENABLED
    _ENABLED = bool(value)


def is_enabled() -> bool:
    return _ENABLED


def beep(kind: str) -> None:
    """Emite una secuencia. Si los avisos estan apagados, no hace nada."""
    if not _ENABLED:
        return
    sequence = _SEQUENCES.get(kind)
    if not sequence:
        log.debug("secuencia de aviso desconocida: %s", kind)
        return

    try:
        import winsound
    except ImportError:  # pragma: no cover - otros sistemas
        return

    for freq, ms in sequence:
        try:
            winsound.Beep(int(freq), int(ms))
        except Exception:  # pragma: no cover - sin tarjeta o dispositivo ocupado
            log.debug("no se pudo emitir el aviso %s", kind)
            return


def beep_async(kind: str) -> None:
    """Igual que `beep`, pero sin bloquear el hilo que llama.

    La interfaz grafica lo necesita: `winsound.Beep` duerme el hilo durante la
    duracion del tono y congelaria la ventana.
    """
    if not _ENABLED:
        return
    threading.Thread(target=beep, args=(kind,), daemon=True).start()
