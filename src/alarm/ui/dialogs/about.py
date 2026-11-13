"""Acerca de la aplicacion y datos de contacto.

El correo de contacto del autor aparece aqui y en `Ayuda → Contacto`, con enlace
`mailto:` y boton de copiar. Copiar importa: en un portatil sin cliente de
correo configurado, el `mailto:` no abre nada y el usuario se queda sin la
direccion a mano.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ... import __version__
from ...paths import fallback_used, is_frozen, storage_root
from .. import icons, theme

CONTACT_EMAIL = "josealberto.vel@gmail.com"
PROJECT_NAME = "Alarma de presencia"
PROTOCOL = "v1 · líneas `TIPO seq k=v`"


class AboutDialog(QDialog):
    """Ficha del proyecto con version, hardware y contacto."""

    def __init__(self, parent: QWidget | None = None, *, simulated: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Acerca de la alarma")
        self.setModal(True)
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 24, 26, 20)
        outer.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(14)
        badge = QLabel()
        badge.setPixmap(icons.app_icon().pixmap(56, 56))
        badge.setFixedSize(60, 60)
        header.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        name = QLabel(PROJECT_NAME)
        name.setFont(theme.font(15, bold=True))
        titles.addWidget(name)
        version = QLabel(f"Versión {__version__} · interfaz PyQt6")
        version.setObjectName("muted")
        titles.addWidget(version)
        mode = QLabel("Modo simulado" if simulated else "Dispositivo real")
        mode.setObjectName("muted")
        titles.addWidget(mode)
        origin = QLabel(_origin_text())
        origin.setObjectName("muted")
        origin.setWordWrap(True)
        titles.addWidget(origin)
        header.addLayout(titles, stretch=1)
        outer.addLayout(header)

        facts = QLabel(
            "Arduino Mega 2560 + sensor HC-SR501 por USB.\n"
            f"Protocolo {PROTOCOL}.\n"
            "Avisos por correo (SMTP), SMS (Twilio o pasarela del operador) y notificación "
            "push al móvil con ntfy, voz SAPI de Windows y sirena con zumbador en la placa."
        )
        facts.setWordWrap(True)
        outer.addWidget(facts)

        contact_title = QLabel("Contacto y soporte")
        contact_title.setFont(theme.font(10, bold=True))
        outer.addWidget(contact_title)

        row = QHBoxLayout()
        row.setSpacing(8)
        link = QLabel(f'<a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a>')
        link.setOpenExternalLinks(False)
        link.linkActivated.connect(self._open_mail)
        link.setToolTip("Abre el cliente de correo. Si no hay ninguno, usa «Copiar».")
        row.addWidget(link, stretch=1)

        copy = QPushButton("Copiar")
        copy.setIcon(icons.icon("copy", theme.ACCENT, 18))
        copy.setToolTip("Copia la dirección al portapapeles.")
        copy.clicked.connect(self._copy)
        row.addWidget(copy)

        email_button = QPushButton("Escribir correo")
        email_button.setIcon(icons.icon("mail", theme.ACCENT, 18))
        email_button.setToolTip("Abre el cliente de correo con el destinatario ya puesto.")
        email_button.clicked.connect(lambda: self._open_mail(CONTACT_EMAIL))
        row.addWidget(email_button)
        outer.addLayout(row)

        self._feedback = QLabel("")
        self._feedback.setObjectName("muted")
        self._feedback.setFont(theme.font(8))
        outer.addWidget(self._feedback)

        note = QLabel(
            "Proyecto personal. Las credenciales de correo, SMS y el token de ntfy nunca se "
            "guardan en el repositorio: viven en `.env`, que está en .gitignore."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        note.setFont(theme.font(8))
        outer.addWidget(note)

        buttons = QDialogButtonBox()
        close = buttons.addButton("Cerrar", QDialogButtonBox.ButtonRole.AcceptRole)
        if close is not None:
            close.setDefault(True)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(CONTACT_EMAIL)
            self._feedback.setText("Dirección copiada al portapapeles.")

    def _open_mail(self, address: str = CONTACT_EMAIL) -> None:
        opened = QDesktopServices.openUrl(QUrl(f"mailto:{address}"))
        if not opened:
            self._feedback.setText(
                f"No hay cliente de correo configurado. Usa «Copiar» o escribe a {CONTACT_EMAIL}."
            )


def _origin_text() -> str:
    """De donde sale el programa y donde guarda los datos."""
    if not is_frozen():
        return f"Código fuente · datos en {storage_root()}"
    if fallback_used():
        return (
            f"Ejecutable portátil · la carpeta del .exe no admite escritura, "
            f"los datos están en {storage_root()}"
        )
    return f"Ejecutable portátil · datos junto al .exe ({storage_root()})"


def data_dir() -> Path:
    """Carpeta de datos de la aplicacion, usada por informes y cache de voz.

    Con el ejecutable empaquetado queda junto a el (o en `%LOCALAPPDATA%` si esa
    carpeta es de solo lectura), nunca en la carpeta temporal de PyInstaller.
    """
    return storage_root() / "data"
