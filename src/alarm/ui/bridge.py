"""Puente entre el hilo de lectura del dispositivo y el hilo de Qt.

Qt, a diferencia de tkinter, admite que una senal se emita desde cualquier hilo
y se entregue en el hilo del receptor (conexion en cola automatica). Por eso el
puente es simplemente un `QObject` con dos senales: el `DeviceManager` llama a
`push_message()` o `push_health()` desde su hilo y la ventana recibe las
llamadas en el suyo, sin bloqueos ni `after()`.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal

from ..protocol.messages import Message

log = logging.getLogger(__name__)


class QtBridge(QObject):
    """Reemite como senales lo que llega por el puerto serie."""

    message = pyqtSignal(object)
    health = pyqtSignal(object)
    notify_result = pyqtSignal(object, str)
    test_finished = pyqtSignal(str, object)
    # Mando remoto: el listener de ntfy vive en su propio hilo y solo emite.
    # La ventana ejecuta la orden en el hilo de Qt, igual que si se pulsara un
    # boton: el controlador y la base de datos no ven dos hilos a la vez.
    remote_command = pyqtSignal(object)
    remote_state = pyqtSignal(object)
    remote_log = pyqtSignal(str, str, str)

    def push_message(self, message: Message) -> None:
        """Seguro desde cualquier hilo."""
        self.message.emit(message)

    def push_health(self, health: object) -> None:
        self.health.emit(health)

    def push_result(self, result: object, cause: str) -> None:
        """Resultado de un envio, emitido desde el hilo del notificador."""
        self.notify_result.emit(result, cause)

    def push_remote_command(self, command: object) -> None:
        self.remote_command.emit(command)

    def push_remote_state(self, state: object) -> None:
        self.remote_state.emit(state)

    def push_remote_log(self, kind: str, title: str, detail: str) -> None:
        self.remote_log.emit(kind, title, detail)
