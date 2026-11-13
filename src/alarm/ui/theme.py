"""Paleta, hoja de estilos e iconos de la interfaz Qt.

Todo el aspecto visual se define aqui para que las ventanas no repitan colores
ni margenes. La hoja de estilos usa nombres de objeto (`#card`, `#heroState`,
`#primary`...) y clases dinamicas de Qt (`property("state")`) en lugar de tocar
paletas widget a widget: cambiar un color es cambiar una linea.
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import QApplication

# Colores neutros y de estado. Los de estado tienen contraste suficiente sobre
# blanco como para usarse como texto, no solo como fondo.
BG = "#f4f5f7"
CARD = "#ffffff"
LINE = "#e1e4e8"
TEXT = "#1b1b1b"
MUTED = "#6b7280"
OK = "#0f7b3f"
WARN = "#b45309"
FAIL = "#c42b1c"
ACCENT = "#0f6cbd"
ACCENT_HOVER = "#115ea3"
SOFT = "#eef4fb"
# Canal de aviso apagado: se ve, pero no compite con el texto de al lado.
OFF = "#a8b0b8"

# Colores de la ilustracion del hardware: la placa, su serigrafia y los
# componentes metalicos. Viven aqui para que el widget no los incruste.
BOARD = "#00979D"
BOARD_DARK = "#007a80"
BOARD_OFF = "#b6bcbf"
BOARD_CHIP = "#2b2f33"
GOLD = "#d4a748"
SILVER = "#b9c0c6"

# Colores para el registro de eventos, por tipo de fila. `triggered` es el
# disparo de la alarma y va en rojo porque es lo unico que el usuario necesita
# encontrar de un vistazo al reconstruir que paso.
LOG_COLORS: dict[str, str] = {
    "start": OK,
    "end": MUTED,
    "bounce": WARN,
    "error": FAIL,
    "triggered": FAIL,
    "rearming": WARN,
    "rearmed": WARN,
    "info": ACCENT,
    "alarm": ACCENT,
    "success": OK,
    "heartbeat": "#9aa0a6",
}

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"


def font(size: int = 10, *, bold: bool = False) -> QFont:
    f = QFont(FONT_FAMILY, size)
    f.setBold(bold)
    return f


def mono(size: int = 9) -> QFont:
    return QFont(MONO_FAMILY, size)


def state_color(key: str, *, silenced: bool = False) -> str:
    """Color del estado de la alarma. Coincide con el semaforo esperado."""
    if key == "armed":
        return OK
    # Los retardos son estados intermedios: ni tranquilos ni disparados. El
    # ambar pide atencion sin gritar, que es lo que toca durante una cuenta
    # atras en la que el usuario tiene que actuar.
    if key in ("arming", "pending", "rearming"):
        return WARN
    if key == "triggered":
        return WARN if silenced else FAIL
    if key == "silenced":
        return WARN
    return MUTED


STYLESHEET = """
QWidget {
    color: #1b1b1b;
    font-family: "Segoe UI";
    font-size: 10pt;
}
QMainWindow, QDialog {
    background: #f4f5f7;
}
QToolTip {
    background: #ffffff;
    color: #1b1b1b;
    border: 1px solid #e1e4e8;
    padding: 6px 8px;
    border-radius: 6px;
}
QFrame#card {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 12px;
}
QFrame#hero {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 16px;
}
QLabel#cardTitle {
    color: #6b7280;
    font-size: 9pt;
    font-weight: 600;
}
QLabel#muted {
    color: #6b7280;
}
QLabel#heroState {
    font-size: 30pt;
    font-weight: 700;
}
QLabel#heroClock {
    font-family: "Consolas";
    font-size: 46pt;
    font-weight: 700;
}
QLabel#heroHint {
    color: #6b7280;
    font-size: 10pt;
}
QLabel#value {
    font-size: 11pt;
    font-weight: 600;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #d0d4da;
    border-radius: 8px;
    padding: 7px 14px;
}
QPushButton:hover {
    background: #f7f9fc;
    border-color: #b9bfc7;
}
QPushButton:pressed {
    background: #eef1f5;
}
QPushButton:disabled {
    color: #9aa0a6;
    background: #f2f3f5;
}
QPushButton#primary {
    background: #0f6cbd;
    color: #ffffff;
    border: none;
    border-radius: 12px;
    padding: 16px 28px;
    font-size: 14pt;
    font-weight: 700;
}
QPushButton#primary:hover {
    background: #115ea3;
}
QPushButton#primary[pstate="armed"] {
    background: #b3261e;
}
QPushButton#primary[pstate="armed"]:hover {
    background: #9a1f19;
}
QPushButton#primary[pstate="triggered"] {
    background: #b45309;
}
QPushButton#danger {
    color: #c42b1c;
    border-color: #e3b4af;
}
QPushButton#danger:hover {
    background: #fdf3f2;
}
/* Boton-accion de conexion: el color es el estado, no una decoracion. */
QPushButton#connect[state="connected"] {
    background: #0f7b3f;
    border-color: #0f7b3f;
    color: #ffffff;
    font-weight: 700;
}
QPushButton#connect[state="connected"]:disabled {
    background: #0f7b3f;
    color: #ffffff;
}
QPushButton#connect[state="retrying"] {
    background: #b45309;
    border-color: #b45309;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#connect[state="retrying"]:disabled {
    background: #b45309;
    color: #ffffff;
}
QPushButton#connect[state="offline"] {
    background: #ffffff;
    border: 1px solid #d0d4da;
    color: #1b1b1b;
}
QPushButton#connect[state="offline"]:hover {
    background: #f7f9fc;
    border-color: #b9bfc7;
}
QPushButton#link {
    background: transparent;
    border: none;
    color: #0f6cbd;
    padding: 2px 4px;
    text-align: left;
}
QPushButton#link:hover {
    text-decoration: underline;
}
QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 6px;
}
QToolButton:hover {
    background: #eef1f5;
    border-color: #dfe3e8;
}
QToolButton:pressed {
    background: #e3e8ee;
}
QToolBar {
    background: #ffffff;
    border: none;
    border-bottom: 1px solid #e1e4e8;
    padding: 4px 8px;
    spacing: 4px;
}
QToolBar::separator {
    background: #e1e4e8;
    width: 1px;
    margin: 4px 8px;
}
QStatusBar {
    background: #ffffff;
    border-top: 1px solid #e1e4e8;
    color: #6b7280;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background: #ffffff;
    border: 1px solid #d0d4da;
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: #cfe4ff;
    selection-color: #1b1b1b;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {
    border-color: #0f6cbd;
}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {
    background: #f2f3f5;
    color: #9aa0a6;
}
QCheckBox {
    spacing: 8px;
}
QGroupBox {
    border: 1px solid #e1e4e8;
    border-radius: 10px;
    margin-top: 14px;
    padding: 12px 10px 10px 10px;
    background: #ffffff;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: #6b7280;
}
QListWidget, QTreeWidget, QTableWidget {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 10px;
    alternate-background-color: #fafbfc;
}
QTreeWidget::item, QListWidget::item {
    padding: 3px 4px;
}
QHeaderView::section {
    background: #f7f8fa;
    color: #6b7280;
    border: none;
    border-bottom: 1px solid #e1e4e8;
    padding: 6px;
    font-weight: 600;
}
QTabWidget::pane {
    border: 1px solid #e1e4e8;
    border-radius: 10px;
    background: #ffffff;
    top: 8px;
}
QTabBar::tab {
    background: transparent;
    color: #6b7280;
    padding: 8px 16px;
    border-radius: 8px;
    margin-right: 4px;
}
QTabBar::tab:selected {
    background: #eef4fb;
    color: #0f6cbd;
    font-weight: 600;
}
QScrollArea {
    border: none;
    background: transparent;
}
QProgressBar {
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    background: #f2f3f5;
    text-align: center;
    height: 10px;
}
QProgressBar::chunk {
    background: #0f6cbd;
    border-radius: 5px;
}
QProgressBar#heroClockBar {
    border: none;
    border-radius: 4px;
    background: #f2f3f5;
    height: 8px;
}
QSplitter::handle {
    background: transparent;
    height: 8px;
}
QMenu {
    background: #ffffff;
    border: 1px solid #e1e4e8;
    border-radius: 8px;
    padding: 6px;
}
QMenu::item {
    padding: 6px 24px 6px 12px;
    border-radius: 6px;
}
QMenu::item:selected {
    background: #eef4fb;
    color: #0f6cbd;
}
QMenu::separator {
    height: 1px;
    background: #e1e4e8;
    margin: 4px 8px;
}
"""


def apply(app: QApplication) -> None:
    """Aplica la paleta y la hoja de estilos a la aplicacion."""
    app.setStyle("Fusion")
    app.setFont(font(10))
    app.setStyleSheet(STYLESHEET)

    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.Base, QColor(CARD))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
