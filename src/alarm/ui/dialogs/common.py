"""Piezas comunes de los dialogos.

Un dialogo de esta aplicacion siempre tiene la misma forma: cabecera con icono y
subtitulo, un cuerpo con campos y una fila de botones. Repetir esa estructura en
seis ventanas es la clase de duplicacion que envejece mal, asi que vive aqui.

Tambien vive aqui `RecipientsEditor`, porque correo y SMS comparten el mismo
patron: una lista de destinatarios que se anaden y se quitan, con validacion.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import icons, theme

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value.strip()))


def valid_phone(value: str) -> bool:
    digits = "".join(ch for ch in value if ch.isdigit())
    return 9 <= len(digits) <= 15


def _make_button(
    box: QDialogButtonBox, text: str, role: QDialogButtonBox.ButtonRole
) -> QPushButton:
    """Anade un boton y garantiza que no es None.

    Los stubs de PyQt6 declaran `addButton(text, role)` como opcionalmente nulo
    porque tambien acepta un `StandardButton`. Con texto siempre devuelve un
    boton, pero el tipo hay que estrecharlo.
    """
    button = box.addButton(text, role)
    assert button is not None  # noqa: S101 - garantia del propio Qt
    return button


class Dialog(QDialog):
    """Base con cabecera, cuerpo en formulario y botonera estandar."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str,
        subtitle: str = "",
        icon_name: str = "gear",
        icon_color: str = theme.ACCENT,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 18)
        outer.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(12)
        badge = QLabel()
        badge.setPixmap(icons.icon(icon_name, icon_color, 40).pixmap(40, 40))
        badge.setFixedSize(44, 44)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(badge, alignment=Qt.AlignmentFlag.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        heading = QLabel(title)
        heading.setFont(theme.font(13, bold=True))
        titles.addWidget(heading)
        if subtitle:
            caption = QLabel(subtitle)
            caption.setObjectName("muted")
            caption.setWordWrap(True)
            caption.setFont(theme.font(9))
            titles.addWidget(caption)
        header.addLayout(titles, stretch=1)
        outer.addLayout(header)

        self._form = QFormLayout()
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self._form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        self._form.setHorizontalSpacing(16)
        self._form.setVerticalSpacing(10)
        self._form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        outer.addLayout(self._form)

        self._buttons = QDialogButtonBox()
        outer.addWidget(self._buttons)

    # -- Construccion ------------------------------------------------------

    def form(self) -> QFormLayout:
        return self._form

    def add_row(self, caption: str, widget: QWidget, hint: str = "") -> QWidget:
        """Anade un campo con etiqueta y, opcionalmente, ayuda debajo."""
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        column.addWidget(widget)
        if hint:
            help_label = QLabel(hint)
            help_label.setObjectName("muted")
            help_label.setFont(theme.font(8))
            help_label.setWordWrap(True)
            column.addWidget(help_label)
        self._form.addRow(caption, holder)
        return widget

    def add_full_row(self, widget: QWidget) -> QWidget:
        self._form.addRow(widget)
        return widget

    def buttons(self) -> QDialogButtonBox:
        return self._buttons

    def add_standard_buttons(self, *, save_text: str = "Guardar") -> None:
        save = _make_button(self._buttons, save_text, QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("primary")
        save.setDefault(True)
        cancel = _make_button(self._buttons, "Cancelar", QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setToolTip("Cierra sin guardar los cambios.")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

    def add_left_button(
        self, text: str, slot: Callable[[], None], tooltip: str = ""
    ) -> QPushButton:
        """Boton secundario alineado a la izquierda (probar, restaurar...)."""
        button = _make_button(self._buttons, text, QDialogButtonBox.ButtonRole.ActionRole)
        button.clicked.connect(slot)
        if tooltip:
            button.setToolTip(tooltip)
        return button


class RecipientsEditor(QWidget):
    """Lista de destinatarios con anadir, quitar y validacion.

    La validacion avisa al usuario en el propio campo: un correo mal escrito o
    un numero sin digitos suficientes no se anade. Es preferible a guardar algo
    que fallara tres dias despues al dispararse la alarma.
    """

    changed = pyqtSignal()

    def __init__(
        self,
        *,
        kind: str = "email",
        placeholder: str = "",
        validator: Callable[[str], bool] = valid_email,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._validator = validator
        self._items = QListWidget()
        self._items.setMaximumHeight(96)
        self._items.setAlternatingRowColors(True)
        self._items.setToolTip(
            "Destinatarios que recibirán el aviso. Cada uno se comprueba al añadirlo."
        )

        self._input = QLineEdit()
        self._input.setPlaceholderText(placeholder)
        self._input.returnPressed.connect(self._add)

        self._add_button = QPushButton("Añadir")
        self._add_button.setToolTip("Añade el valor escrito a la lista.")
        self._add_button.clicked.connect(self._add)

        self._remove_button = QPushButton("Quitar")
        self._remove_button.setObjectName("danger")
        self._remove_button.setToolTip("Quita el destinatario seleccionado.")
        self._remove_button.clicked.connect(self._remove)

        self._error = QLabel("")
        self._error.setStyleSheet(f"color: {theme.FAIL}; font-size: 8pt;")

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self._input, stretch=1)
        row.addWidget(self._add_button)

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addLayout(row)
        column.addWidget(self._items)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addWidget(self._error, stretch=1)
        bottom.addWidget(self._remove_button)
        column.addLayout(bottom)

    # -- Datos -------------------------------------------------------------

    def values(self) -> list[str]:
        result: list[str] = []
        for index in range(self._items.count()):
            item = self._items.item(index)
            if item is not None:
                result.append(item.text())
        return result

    def set_values(self, values: list[str] | tuple[str, ...]) -> None:
        self._items.clear()
        for value in values:
            if value.strip():
                self._items.addItem(value.strip())
        self._error.setText("")
        self.changed.emit()

    def add_value(self, value: str) -> bool:
        """Anade un valor ya validado. Devuelve si lo acepto."""
        self._input.setText(value)
        self._add()
        accepted = not self._error.text()
        self._input.clear()
        return accepted

    def commit_pending(self) -> bool:
        """Anade lo escrito si es valido y deja el campo vacio.

        Sin esto, teclear el numero y pulsar «Probar» o «Guardar» sin pasar
        por «Anadir» hacia que la aplicacion dijera que faltan destinatarios
        mientras el usuario los estaba viendo escritos.
        """
        if not self._input.text().strip():
            return True
        self._add()
        return not self._error.text()

    def pending(self) -> str:
        """Lo escrito y aun sin anadir, solo si es valido.

        Sirve para avisar en vivo sin anadir a medias lo que el usuario esta
        tecleando.
        """
        value = self._input.text().strip()
        return value if value and self._validator(value) else ""

    def error_text(self) -> str:
        return self._error.text()

    def _add(self) -> None:
        value = self._input.text().strip()
        if not value:
            return
        if not self._validator(value):
            what = "un correo válido" if self._kind == "email" else "un número válido"
            self._error.setText(f"«{value}» no parece {what}.")
            return
        if value in self.values():
            self._error.setText("Ese destinatario ya está en la lista.")
            return
        self._items.addItem(value)
        self._input.clear()
        self._error.setText("")
        self.changed.emit()

    def _remove(self) -> None:
        row = self._items.currentRow()
        if row >= 0:
            self._items.takeItem(row)
            self._error.setText("")
            self.changed.emit()

    @property
    def kind(self) -> str:
        return self._kind
