"""Piezas reutilizables de la interfaz.

Son widgets pequenos y sin logica de negocio: tarjetas, cabecera de estado y
registro de eventos. La ventana principal los compone; los dialogos reutilizan
las tarjetas y los botones.

Cada pieza recibe su contenido por metodo (`set_state`, `add_row`...) para que el
estado se pinte desde un unico sitio y no repartido por la ventana.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Iterable

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import icons, theme

#: Papel que guarda la clave de ordenacion de una fila del registro.
_SORT_ROLE = Qt.ItemDataRole.UserRole


def tip(widget: QWidget, text: str) -> QWidget:
    """Tooltip con salto de linea opcional y ancho razonable."""
    widget.setToolTip(text)
    widget.setToolTipDuration(12000)
    return widget


def label(text: str = "", *, name: str = "", bold: bool = False, size: int = 10) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setFont(theme.font(size, bold=bold))
    return widget


class Card(QFrame):
    """Panel blanco con borde redondeado y titulo opcional."""

    def __init__(self, title: str = "", *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(18, 16, 18, 16)
        self._layout.setSpacing(10)
        self._title_row: QHBoxLayout | None = None
        if title:
            self._title_row = QHBoxLayout()
            self._title_row.setContentsMargins(0, 0, 0, 0)
            self._title_row.setSpacing(8)
            self._title = QLabel(title.upper())
            self._title.setObjectName("cardTitle")
            self._title.setFont(theme.font(8, bold=True))
            self._title_row.addWidget(self._title)
            self._title_row.addStretch(1)
            self._layout.addLayout(self._title_row)

    def add_header(self, widget: QWidget) -> QWidget:
        """Anade una accion en la fila del titulo, alineada a la derecha."""
        if self._title_row is None:
            self._title_row = QHBoxLayout()
            self._title_row.setContentsMargins(0, 0, 0, 0)
            self._title_row.setSpacing(8)
            self._title_row.addStretch(1)
            self._layout.insertLayout(0, self._title_row)
        self._title_row.addWidget(widget)
        return widget

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget, *, stretch: int = 0) -> QWidget:
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout: QHBoxLayout) -> QHBoxLayout:
        self._layout.addLayout(layout)
        return layout


class StateHero(QFrame):
    """Cabecera grande: icono de estado, etiqueta y pista de que hacer.

    Es lo primero que se mira y lo que debe responder a "¿esta puesta la
    alarma?" sin leer nada mas.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("hero")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._icon = QLabel()
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setFixedSize(64, 64)
        layout.addWidget(self._icon, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._state = QLabel("DESARMADA")
        self._state.setObjectName("heroState")
        self._state.setFont(theme.hero_font())
        self._state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._state)

        # Reloj grande del retardo. Solo aparece mientras hay cuenta atras, asi
        # que no ocupa sitio en el uso normal ni encoge el resto del hero.
        self._clock = QLabel()
        self._clock.setObjectName("heroClock")
        self._clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._clock.setVisible(False)
        layout.addWidget(self._clock)

        self._clock_bar = QProgressBar()
        self._clock_bar.setObjectName("heroClockBar")
        self._clock_bar.setRange(0, 1000)
        self._clock_bar.setTextVisible(False)
        self._clock_bar.setFixedHeight(8)
        self._clock_bar.setMaximumWidth(320)
        self._clock_bar.setVisible(False)
        layout.addWidget(self._clock_bar, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._hint = QLabel("La alarma está apagada y no avisará de movimiento.")
        self._hint.setObjectName("heroHint")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        layout.addSpacing(6)
        self._buttons = QHBoxLayout()
        self._buttons.setSpacing(10)
        self._buttons.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addLayout(self._buttons)
        layout.addStretch(1)

    def add_button(self, button: QWidget) -> QWidget:
        self._buttons.addWidget(button)
        return button

    def set_state(self, key: str, text: str, hint: str, *, silenced: bool = False) -> None:
        color = theme.state_color(key, silenced=silenced)
        self._state.setText(text)
        self._state.setStyleSheet(f"color: {color};")
        self._hint.setText(hint)
        self._icon.setPixmap(icons.state_icon(key, color, 64).pixmap(64, 64))

    def set_hint(self, text: str) -> None:
        """Cambia solo la pista: la cuenta atras se repinta sin tocar icono ni color."""
        self._hint.setText(text)

    def set_countdown(self, remaining_s: float, total_s: float, color: str) -> None:
        """Pinta el reloj grande del retardo y su barra de avance.

        El numero se redondea hacia arriba: con 0,4 s restantes aun se lee 1,
        que es mas honesto que mostrar 0 mientras el zumbador sigue pitando.
        La barra mide lo que **queda**, asi que se vacia a medida que el plazo
        se agota y el usuario ve de un vistazo cuanto margen le queda.
        """
        remaining = max(0, math.ceil(remaining_s))
        minutes, seconds = divmod(remaining, 60)
        self._clock.setText(f"{minutes:02d}:{seconds:02d}")
        self._clock.setStyleSheet(f"color: {color};")
        self._clock.setVisible(True)

        fraction = 0.0 if total_s <= 0 else remaining_s / total_s
        self._clock_bar.setValue(max(0, min(1000, round(fraction * 1000))))
        self._clock_bar.setStyleSheet(
            "QProgressBar#heroClockBar { background: #f2f3f5; border: none;"
            " border-radius: 4px; }"
            f"QProgressBar#heroClockBar::chunk {{ background: {color};"
            " border-radius: 4px; }"
        )
        self._clock_bar.setVisible(True)

    def clear_countdown(self) -> None:
        """Oculta el reloj cuando no hay retardo en curso."""
        self._clock.setVisible(False)
        self._clock_bar.setVisible(False)

    def set_icon(self, name: str, color: str) -> None:
        self._icon.setPixmap(icons.icon(name, color, 64).pixmap(64, 64))


class IconButton(QToolButton):
    """Boton de barra de herramientas con icono y descripcion emergente."""

    def __init__(
        self,
        icon_name: str,
        text: str,
        tooltip: str,
        *,
        color: str = theme.TEXT,
        size: int = 22,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setIcon(icons.icon(icon_name, color, size))
        self.setText(text)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        tip(self, tooltip)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)


class _LogItem(QTreeWidgetItem):
    """Fila del registro que se ordena por su dato, no por el texto pintado.

    La hora se ve como `HH:MM:SS.mmm`, que ordenada como texto se rompe al
    cruzar la medianoche. Se guarda la marca completa en `_SORT_ROLE`.
    """

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        if column == 0:
            mine = self.data(0, _SORT_ROLE)
            theirs = other.data(0, _SORT_ROLE)
            if mine is not None and theirs is not None:
                return tuple(mine) < tuple(theirs)
        return self.text(column).casefold() < other.text(column).casefold()


class EventLog(QTreeWidget):
    """Registro cronologico de eventos, coloreado por tipo.

    Se limita el numero de filas visibles: el historial completo vive en SQLite,
    pero un arbol con cien mil nodos se arrastra. El limite es inyectable
    (`set_limit`) porque lo fija la configuracion (`[log].visible_rows`).

    La tabla se puede ordenar pulsando la cabecera. El recorte de filas sigue
    el orden de **llegada** (un `deque`), no la fila visible: si el usuario
    ordena por detalle, la fila 0 puede no ser la mas antigua.
    """

    MAX_ROWS = 800
    DEFAULT_LIMIT = 1_000
    HARD_LIMIT = 5_000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._limit = min(self.DEFAULT_LIMIT, self.HARD_LIMIT)
        self._items: deque[_LogItem] = deque()
        self._sequence = 0
        self.setColumnCount(3)
        self.setHeaderLabels(["Hora", "Evento", "Detalle"])
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.setAllColumnsShowFocus(True)
        header = self.header()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            header.setSortIndicatorShown(True)
            header.setSectionsClickable(True)
        tip(
            self,
            "Historial de lo que ha ocurrido: detecciones, avisos y errores. "
            "Pulsa una cabecera para ordenar por esa columna.",
        )
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.SortOrder.AscendingOrder)

    def set_limit(self, limit: int) -> None:
        """Filas maximas visibles, acotadas al rango razonable del arbol."""
        self._limit = max(1, min(int(limit), self.HARD_LIMIT))
        self._trim()

    @property
    def limit(self) -> int:
        return self._limit

    def add_row(
        self, tag: str, kind: str, detail: str, *, ts: float | None = None, scroll: bool = True
    ) -> None:
        stamp = time.time() if ts is None else ts
        text = time.strftime("%H:%M:%S", time.localtime(stamp))
        text = f"{text}.{int((stamp % 1) * 1000):03d}"
        item = _LogItem([text, kind, detail])
        self._sequence += 1
        item.setData(0, _SORT_ROLE, (stamp, self._sequence))
        color = QColor(theme.LOG_COLORS.get(tag, theme.TEXT))
        for column in range(3):
            item.setForeground(column, color)
        if tag in ("error", "alarm", "triggered", "rearmed"):
            font = QFont(self.font())
            font.setBold(True)
            for column in range(3):
                item.setFont(column, font)
        # Con la ordenacion activa, Qt inserta la fila en su sitio con el
        # comparador de `_LogItem`: no hace falta desactivarla ni reordenar.
        self.addTopLevelItem(item)
        self._items.append(item)
        self._trim()
        if scroll:
            self._scroll_to(item)

    def _scroll_to(self, item: QTreeWidgetItem) -> None:
        """Baja al final solo si la tabla esta en orden cronologico ascendente."""
        header = self.header()
        if self.sortColumn() != 0 or header is None:
            return
        if header.sortIndicatorOrder() is Qt.SortOrder.DescendingOrder:
            return
        self.scrollToItem(item)

    def _trim(self) -> None:
        """Descarta las filas mas antiguas, en orden de llegada."""
        while len(self._items) > self._limit:
            old = self._items.popleft()
            index = self.indexOfTopLevelItem(old)
            if index >= 0:
                self.takeTopLevelItem(index)

    def rows(self) -> list[tuple[str, str, str]]:
        """Filas actuales como tuplas, para pruebas y exportaciones."""
        result: list[tuple[str, str, str]] = []
        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            if item is None:
                continue
            result.append((item.text(0), item.text(1), item.text(2)))
        return result

    def joined(self) -> str:
        return " | ".join(" ".join(row) for row in self.rows())

    def clear_rows(self) -> None:
        self._items.clear()
        self.clear()


def separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"color: {theme.LINE}; background: {theme.LINE}; max-height: 1px;")
    return line


def kv_row(pairs: Iterable[tuple[str, QWidget]]) -> QHBoxLayout:
    """Fila de pares etiqueta/valor, con separacion uniforme."""
    layout = QHBoxLayout()
    layout.setSpacing(24)
    for caption, widget in pairs:
        column = QVBoxLayout()
        column.setSpacing(2)
        heading = label(caption, name="muted")
        heading.setFont(theme.font(8))
        column.addWidget(heading)
        column.addWidget(widget)
        layout.addLayout(column)
    layout.addStretch(1)
    return layout
