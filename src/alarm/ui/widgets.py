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
from collections.abc import Callable, Iterable

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QFont, QResizeEvent
from PyQt6.QtWidgets import (
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QToolBar,
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

    Es tambien la pieza que mas sufre al estrechar la ventana: el rotulo esta
    pensado para leerse de lejos y, a cuerpo fijo, se salia de la tarjeta
    («DESARMADA» no cabia con la ventana en su tamano minimo). Por eso el
    cuerpo del rotulo y el del reloj se ajustan al ancho disponible, y las
    acciones pasan a una columna cuando en una fila se recortarian sus
    etiquetas.
    """

    #: Cuerpo del rotulo del estado. El maximo es el de la hoja de estilos; a
    #: partir de ahi solo se encoge, nunca se hincha: la jerarquia del hero no
    #: cambia por tener una pantalla mas grande.
    STATE_PT_MAX = 30
    STATE_PT_MIN = 13
    #: Cuerpo del reloj de cuenta atras, con el mismo criterio. En modo columna
    #: se queda mas pequeno: apilado, al hero no le sobra altura.
    CLOCK_PT_MAX = 46
    CLOCK_PT_MIN = 20
    CLOCK_PT_COLUMN = 32
    #: Aire que se deja entre el texto y el borde de la tarjeta.
    SIDE_PAD = 8
    #: Ancho minimo de la tarjeta. Por debajo deja de ser la pieza principal de
    #: la ventana: el rotulo se vuelve diminuto y las acciones no caben. Antes
    #: de eso, que ceda la tarjeta del dispositivo.
    MIN_WIDTH = 400
    #: Holgura normal y holgura cuando las acciones van en columna: apiladas
    #: ocupan el triple de alto y hay que devolverle ese espacio al resto.
    MARGINS = (28, 24, 28, 24)
    MARGINS_COLUMN = (20, 12, 20, 12)
    #: Tamano del icono de estado en cada modo.
    ICON = 64
    ICON_COLUMN = 44
    #: Separacion entre elementos, en fila y en columna.
    SPACING = 10
    SPACING_COLUMN = 6
    #: Alto de las acciones apiladas. En fila manda el que traiga el boton: el
    #: hero no le impone su diseno a quien lo usa.
    BUTTON_COLUMN = 46

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("hero")
        self.setMinimumWidth(self.MIN_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.MARGINS)
        layout.setSpacing(self.SPACING)
        self._layout = layout

        self._icon = QLabel()
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setFixedSize(self.ICON, self.ICON)
        layout.addWidget(self._icon, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._state = QLabel("DESARMADA")
        self._state.setObjectName("heroState")
        self._state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # El rotulo no dicta el ancho del hero. Si su `sizeHint` entrara en el
        # minimo, encoger la letra ensancharia la tarjeta y la medida del
        # ajuste se quedaria siempre un paso por detras de la geometria.
        self._state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._state)

        # Reloj grande del retardo. Solo aparece mientras hay cuenta atras, asi
        # que no ocupa sitio en el uso normal ni encoge el resto del hero.
        self._clock = QLabel()
        self._clock.setObjectName("heroClock")
        self._clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._clock.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
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
        # Fila de acciones que se convierte en columna si no caben las
        # etiquetas. `QBoxLayout` permite cambiar de direccion en caliente; un
        # `QHBoxLayout` obligaria a reconstruir el widget en cada ajuste.
        self._buttons = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._buttons.setSpacing(self.SPACING)
        self._buttons.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addLayout(self._buttons)
        layout.addStretch(1)

        self._state_pt = self.STATE_PT_MAX
        self._clock_pt = self.CLOCK_PT_MAX
        self._state_color = theme.MUTED
        self._clock_color = theme.MUTED
        self._icon_key = ""
        self._icon_color = theme.MUTED
        # Alto que traia cada boton al anadirse, para devolverselo al volver a
        # la fila.
        self._heights: dict[QWidget, int] = {}
        self._column = False
        # Textos, ancho y modo del ultimo ajuste, para no repetir la busqueda en
        # cada tic de la cuenta atras.
        self._fitted: tuple[str, str, int, bool] | None = None
        self._paint_state()
        self._paint_clock()

    def add_button(self, button: QWidget) -> QWidget:
        self._buttons.addWidget(button)
        self._heights[button] = button.minimumHeight()
        self._fitted = None
        self._fit()
        return button

    def set_state(self, key: str, text: str, hint: str, *, silenced: bool = False) -> None:
        color = theme.state_color(key, silenced=silenced)
        self._state_color = color
        self._state.setText(text)
        self._hint.setText(hint)
        self._icon_key = key
        self._paint_icon(color)
        self._fit()

    # ------------------------------------------------------------------
    # Ajuste al ancho disponible
    # ------------------------------------------------------------------

    def resizeEvent(self, event: QResizeEvent | None) -> None:  # noqa: N802 - API de Qt
        super().resizeEvent(event)
        self._fit()

    def _available(self, margins: tuple[int, int, int, int] | None = None) -> int:
        """Ancho util para el texto y las acciones, ya descontados los margenes."""
        if margins is None:
            margins = self.MARGINS_COLUMN if self._column else self.MARGINS
        return max(0, self.width() - margins[0] - margins[2] - self.SIDE_PAD)

    def _buttons_list(self) -> list[QWidget]:
        botones: list[QWidget] = []
        for index in range(self._buttons.count()):
            item = self._buttons.itemAt(index)
            widget = item.widget() if item is not None else None
            if widget is not None:
                botones.append(widget)
        return botones

    def _fit(self) -> None:
        if self.width() <= 0:
            return
        self._set_column(self._needs_column())
        width = self._available()
        if width <= 0:
            return
        key = (self._state.text(), self._clock.text(), width, self._column)
        if key == self._fitted:
            return
        self._fitted = key
        self._fit_label(self._state, self._paint_state, self.STATE_PT_MAX, self.STATE_PT_MIN, width)
        clock_max = self.CLOCK_PT_COLUMN if self._column else self.CLOCK_PT_MAX
        self._fit_label(self._clock, self._paint_clock, clock_max, self.CLOCK_PT_MIN, width)

    def _needs_column(self) -> bool:
        """Decide si las acciones caben en una fila con la holgura normal.

        La decision se toma siempre contra los margenes normales, tambien con
        las acciones ya apiladas: si se midiera con la holgura recortada, el
        ancho justo podria hacer que la fila quepa, se desapile, vuelva a no
        caber y la ventana se quedara oscilando entre los dos modos.
        """
        buttons = self._buttons_list()
        if not buttons:
            return False
        needed = sum(button.sizeHint().width() for button in buttons)
        needed += self.SPACING * (len(buttons) - 1)
        return needed > self._available(self.MARGINS)

    def _set_column(self, column: bool) -> None:
        """Pasa las acciones a columna (o las devuelve a fila) y ajusta el resto."""
        if column == self._column:
            return
        self._column = column
        self._layout.setContentsMargins(*(self.MARGINS_COLUMN if column else self.MARGINS))
        self._layout.setSpacing(self.SPACING_COLUMN if column else self.SPACING)
        self._buttons.setSpacing(self.SPACING_COLUMN if column else self.SPACING)
        self._buttons.setDirection(
            QBoxLayout.Direction.TopToBottom if column else QBoxLayout.Direction.LeftToRight
        )
        size = self.ICON_COLUMN if column else self.ICON
        self._icon.setFixedSize(size, size)
        self._paint_icon(self._icon_color)
        for button in self._buttons_list():
            height = self._heights[button]
            button.setMinimumHeight(min(self.BUTTON_COLUMN, height) if column else height)
        # El modo cambia el ancho util y el tamano del icono: toca reajustar.
        self._fitted = None

    def _paint_icon(self, color: str) -> None:
        self._icon_color = color
        size = self.ICON_COLUMN if self._column else self.ICON
        self._icon.setPixmap(icons.state_icon(self._icon_key, color, size).pixmap(size, size))

    def _fit_label(
        self,
        label: QLabel,
        paint: Callable[[int], None],
        maximum: int,
        minimum: int,
        width: int,
    ) -> None:
        """Deja la etiqueta en el cuerpo mayor que quepa en `width`.

        Se busca por biseccion porque cada prueba vuelve a aplicar la hoja de
        estilos: medir con `QFont` no vale, que es el estilo quien manda sobre
        el cuerpo del `QLabel` y no `setFont`.
        """
        low, high, best = minimum, maximum, minimum
        while low <= high:
            middle = (low + high) // 2
            paint(middle)
            if label.fontMetrics().horizontalAdvance(label.text()) <= width:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        paint(best)

    def _paint_state(self, size: int | None = None) -> None:
        if size is not None:
            self._state_pt = size
        self._state.setStyleSheet(f"color: {self._state_color}; font-size: {self._state_pt}pt;")

    def _paint_clock(self, size: int | None = None) -> None:
        if size is not None:
            self._clock_pt = size
        self._clock.setStyleSheet(f"color: {self._clock_color}; font-size: {self._clock_pt}pt;")

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
        self._clock_color = color
        self._clock.setText(f"{minutes:02d}:{seconds:02d}")
        self._clock.setVisible(True)
        self._fit()

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
        """Pinta un icono concreto, sin pasar por la tabla de estados."""
        size = self.ICON_COLUMN if self._column else self.ICON
        self._icon.setPixmap(icons.icon(name, color, size).pixmap(size, size))


class IconButton(QToolButton):
    """Boton de barra de herramientas con icono y descripcion emergente.

    Guarda el icono de origen para poder redibujarlo al tamano que pida la
    barra: escalar un `QPixmap` ya rasterizado deja el trazo sucio, y estas
    barras cambian de tamano con la ventana.
    """

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
        self._icon_name = icon_name
        self._icon_color = color
        self._icon_size = size
        self.setIcon(icons.icon(icon_name, color, size))
        self.setIconSize(QSize(size, size))
        self.setText(text)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        tip(self, tooltip)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def set_icon_size(self, size: int) -> None:
        """Redibuja el icono al tamano pedido por la barra."""
        if size == self._icon_size:
            return
        self._icon_size = size
        self.setIconSize(QSize(size, size))
        self.setIcon(icons.icon(self._icon_name, self._icon_color, size))


class AdaptiveToolBar(QToolBar):
    """Barra de acciones que encoge sus iconos antes que esconder botones.

    Qt, cuando el contenido no cabe, deja media barra ocupada y guarda el resto
    en un menu «»» invisible en la practica. Aqui se hace al reves: primero se
    reduce el tamano del icono y, si aun no cabe, se sueltan las etiquetas (que
    siguen en la descripcion emergente y en el menu). Asi todas las acciones
    quedan a un clic en cualquier tamano de ventana y los iconos acompanan al
    ancho disponible. En una pantalla muy ancha vuelven las etiquetas.

    `resizeEvent` es el unico disparador: no hay que llamar a `fit()` a mano.
    """

    #: Tamanos que se prueban, del mayor al menor. Se para en el primero que
    #: quepa, asi que el resultado es el icono mas grande posible.
    SIZES = (26, 24, 22, 20, 18, 16)

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self._applied: tuple[Qt.ToolButtonStyle, int] | None = None
        self._rendered = 0
        # Ancho que pide cada combinacion, medido una vez por conjunto de
        # etiquetas (cambian con «Conectar»/«Desconectar»).
        self._needed: dict[tuple[Qt.ToolButtonStyle, int, tuple[str, ...]], int] = {}

    def resizeEvent(self, event: QResizeEvent | None) -> None:  # noqa: N802 - API de Qt
        super().resizeEvent(event)
        self.fit()

    def fit(self) -> None:
        """Elige el mayor icono que deje todas las acciones a la vista."""
        width = self.width()
        if width <= 0:
            return
        text_style = Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        icon_style = Qt.ToolButtonStyle.ToolButtonIconOnly
        candidates = [(text_style, size) for size in self.SIZES]
        candidates += [(icon_style, size) for size in self.SIZES]
        chosen = candidates[-1]
        for candidate in candidates:
            if self._needed_width(*candidate) <= width:
                chosen = candidate
                break
        self._apply(*chosen)

    def _needed_width(self, style: Qt.ToolButtonStyle, size: int) -> int:
        texts = tuple(action.text() for action in self.actions())
        key = (style, size, texts)
        cached = self._needed.get(key)
        if cached is None:
            # Medir obliga a ponerse en esa configuracion; `_apply` la deja
            # luego en la elegida. `sizeHint` ya la refleja sin esperar al
            # ciclo de eventos, que dentro de `resizeEvent` no llegaria.
            self._install(style, size)
            cached = self.sizeHint().width()
            self._needed[key] = cached
        return cached

    def _install(self, style: Qt.ToolButtonStyle, size: int) -> None:
        if self.toolButtonStyle() != style:
            self.setToolButtonStyle(style)
        if self.iconSize().width() != size:
            self.setIconSize(QSize(size, size))

    def _apply(self, style: Qt.ToolButtonStyle, size: int) -> None:
        self._applied = (style, size)
        self._install(style, size)
        self._render_icons(size)
        for button in self.findChildren(IconButton):
            button.set_icon_size(size)

    def _render_icons(self, size: int) -> None:
        """Vuelve a dibujar los iconos al tamano nuevo, sin escalar pixeles."""
        if size == self._rendered:
            return
        self._rendered = size
        for action in self.actions():
            name = action.property("iconName")
            if not name:
                continue
            color = action.property("iconColor") or theme.TEXT
            action.setIcon(icons.icon(str(name), str(color), size))


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
