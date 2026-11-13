"""Modal de resultado de una prueba de envio.

Las pruebas de correo, SMS y móvil corren en un hilo para no congelar la ventana.
Cuando terminan, este cuadro cuenta que paso: si fue bien, a quien llego; si
fallo, por que y con un boton para reintentar.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .. import icons, theme


class ResultDialog(QDialog):
    """Resultado de una operacion con reintento opcional."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        title: str,
        ok: bool,
        detail: str = "",
        on_retry: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(440)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 16)
        outer.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(12)
        badge = QLabel()
        color = theme.OK if ok else theme.FAIL
        badge.setPixmap(icons.icon("check" if ok else "warning", color, 34).pixmap(34, 34))
        badge.setFixedSize(38, 38)
        head.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)

        texts = QVBoxLayout()
        texts.setSpacing(4)
        heading = QLabel("Enviado" if ok else "No se pudo enviar")
        heading.setFont(theme.font(12, bold=True))
        heading.setStyleSheet(f"color: {color};")
        texts.addWidget(heading)
        message = QLabel(detail or ("La prueba se completó correctamente." if ok else ""))
        message.setWordWrap(True)
        message.setObjectName("muted")
        texts.addWidget(message)
        head.addLayout(texts, stretch=1)
        outer.addLayout(head)

        buttons = QDialogButtonBox()
        if on_retry is not None and not ok:

            def do_retry() -> None:
                self.accept()
                on_retry()

            retry = buttons.addButton("Reintentar", QDialogButtonBox.ButtonRole.ActionRole)
            if retry is not None:
                retry.setToolTip("Vuelve a intentar el envío con la misma configuración.")
                retry.clicked.connect(do_retry)
        close = buttons.addButton("Cerrar", QDialogButtonBox.ButtonRole.AcceptRole)
        if close is not None:
            close.setDefault(True)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)
