"""Iconos vectoriales de la interfaz.

Se guardan como SVG en linea y se pintan con `QSvgRenderer` para poder
colorearlos en tiempo de ejecucion: el mismo escudo se dibuja gris, verde o rojo
segun el estado, sin necesidad de un fichero por color.

Cada icono es una cadena SVG de 24x24 con `currentColor` como color de trazo.
`icon()` sustituye ese marcador por el color pedido, lo rasteriza al tamano
necesario (con la densidad de pantalla) y devuelve un `QIcon`.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

ASSETS = Path(__file__).resolve().parent / "assets"

_SVG: dict[str, str] = {
    "shield": """
        <path d="M12 3 5 5.8v5.4c0 4.1 2.9 7.6 7 9.8 4.1-2.2 7-5.7 7-9.8V5.8z"/>
        <path d="M9 12l2 2 4-4.5"/>
    """,
    "shield_off": """
        <path d="M12 3 5 5.8v5.4c0 4.1 2.9 7.6 7 9.8 4.1-2.2 7-5.7 7-9.8V5.8z"/>
        <path d="M9.5 9.5l5 5M14.5 9.5l-5 5"/>
    """,
    "shield_alert": """
        <path d="M12 3 5 5.8v5.4c0 4.1 2.9 7.6 7 9.8 4.1-2.2 7-5.7 7-9.8V5.8z"/>
        <path d="M12 8v4.5M12 15.6v.4"/>
    """,
    "bell": """
        <path d="M18 15.5V11a6 6 0 1 0-12 0v4.5L4.5 18h15z"/>
        <path d="M10 20.5a2.2 2.2 0 0 0 4 0"/>
    """,
    "bell_off": """
        <path d="M18 15.5V11a6 6 0 0 0-9.2-5.1M6 7.5V11l-1.5 7h13"/>
        <path d="M4 4l16 16"/>
    """,
    "mail": """
        <rect x="3" y="5.5" width="18" height="13" rx="2.5"/>
        <path d="M3.8 7.2 12 13l8.2-5.8"/>
    """,
    "sms": """
        <path d="M20.5 12.6c0 3.3-3.6 6-8 6-1 0-2-.1-2.9-.4L5 20.5l1.2-3.4A5.8 5.8 0 0 1 4.5 12.6c0-3.3 3.6-6 8-6s8 2.7 8 6z"/>
        <path d="M9 12h.01M12 12h.01M15 12h.01"/>
    """,
    "phone": """
        <rect x="7" y="2.8" width="10" height="18.4" rx="2.4"/>
        <path d="M10.6 18.6h2.8"/>
    """,
    "mic": """
        <rect x="9.3" y="3" width="5.4" height="10.2" rx="2.7"/>
        <path d="M6 11.5a6 6 0 0 0 12 0M12 17.5V21M9 21h6"/>
    """,
    "gear": """
        <circle cx="12" cy="12" r="3.2"/>
        <path d="M19.4 14.4a1.6 1.6 0 0 0 .3 1.8l.1.1a1.9 1.9 0 1 1-2.7 2.7l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5v.3a1.9 1.9 0 1 1-3.8 0v-.2a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a1.9 1.9 0 1 1-2.7-2.7l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1h-.3a1.9 1.9 0 1 1 0-3.8h.2a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a1.9 1.9 0 1 1 2.7-2.7l.1.1a1.6 1.6 0 0 0 1.8.3h.1a1.6 1.6 0 0 0 1-1.5v-.3a1.9 1.9 0 1 1 3.8 0v.2a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a1.9 1.9 0 1 1 2.7 2.7l-.1.1a1.6 1.6 0 0 0-.3 1.8v.1a1.6 1.6 0 0 0 1.5 1h.3a1.9 1.9 0 1 1 0 3.8h-.2a1.6 1.6 0 0 0-1.5 1z"/>
    """,
    "usb": """
        <rect x="4" y="8" width="16" height="9" rx="2"/>
        <path d="M8 8V6h8v2M8 12h8"/>
    """,
    "play": """
        <circle cx="12" cy="12" r="8.5"/>
        <path d="M10 8.7l5 3.3-5 3.3z"/>
    """,
    "refresh": """
        <path d="M20 11a8 8 0 1 0-1.6 5.4"/>
        <path d="M20 4.5V11h-6.2"/>
    """,
    "activity": """
        <path d="M3 12.5h3.6L9 6l3 12 2.6-6h6.4"/>
    """,
    "info": """
        <circle cx="12" cy="12" r="8.5"/>
        <path d="M12 11v5M12 8.2v.4"/>
    """,
    "copy": """
        <rect x="8.5" y="8.5" width="11" height="11" rx="2"/>
        <path d="M15.5 8.5v-2a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2"/>
    """,
    "check": """
        <path d="M4.5 12.5l5 5L20 6.5"/>
    """,
    "close": """
        <path d="M6 6l12 12M18 6L6 18"/>
    """,
    "save": """
        <path d="M5 4.5h11L19.5 8v11.5H5z"/>
        <path d="M8.5 4.5v5h7v-5M8.5 19.5v-6h7v6"/>
    """,
    "download": """
        <path d="M12 4v10.5"/>
        <path d="M7.5 10.5L12 15l4.5-4.5"/>
        <path d="M5 19.5h14"/>
    """,
    "trash": """
        <path d="M4.5 7h15"/>
        <path d="M9.5 7V5.2h5V7"/>
        <path d="M6.5 7l.9 12.1a1.5 1.5 0 0 0 1.5 1.4h6.2a1.5 1.5 0 0 0 1.5-1.4L17.5 7"/>
        <path d="M10.5 10.8v5.6M13.5 10.8v5.6"/>
    """,
    "power": """
        <path d="M12 3.5v8"/>
        <path d="M7.2 6.4a7 7 0 1 0 9.6 0"/>
    """,
    "clock": """
        <circle cx="12" cy="12" r="8.5"/>
        <path d="M12 7.3V12l3.2 2"/>
    """,
    "warning": """
        <path d="M12 4l8.5 15h-17z"/>
        <path d="M12 9.5v4.5M12 17v.4"/>
    """,
    "lock": """
        <rect x="5.5" y="10.5" width="13" height="9.5" rx="2"/>
        <path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>
    """,
    "unlock": """
        <rect x="5.5" y="10.5" width="13" height="9.5" rx="2"/>
        <path d="M8.5 10.5V8a3.5 3.5 0 0 1 6.7-1.5"/>
    """,
    "key": """
        <circle cx="8.5" cy="12" r="3.6"/>
        <path d="M12.1 12h8.4M17.2 12v3M20 12v2.2"/>
    """,
}


def _svg_markup(name: str, color: str, stroke: float) -> bytes:
    body = _SVG.get(name)
    if body is None:
        body = _SVG["info"]
    markup = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="{stroke}" '
        f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )
    return markup.encode("utf-8")


def pixmap(name: str, color: str, size: int = 24, *, stroke: float = 1.7) -> QPixmap:
    """Rasteriza un icono al tamano pedido, con la densidad de pantalla actual."""
    renderer = QSvgRenderer(QByteArray(_svg_markup(name, color, stroke)))
    from PyQt6.QtGui import QGuiApplication

    ratio = 1.0
    if QGuiApplication.instance() is not None:
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            ratio = screen.devicePixelRatio()
    canvas = max(8, int(size * max(1.0, ratio)))
    image = QPixmap(canvas, canvas)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, canvas, canvas))
    painter.end()
    image.setDevicePixelRatio(max(1.0, ratio))
    return image


def icon(name: str, color: str = "#1b1b1b", size: int = 24, *, stroke: float = 1.7) -> QIcon:
    return QIcon(pixmap(name, color, size, stroke=stroke))


def state_icon(key: str, color: str, size: int = 24) -> QIcon:
    """Icono coherente con el estado de la alarma."""
    name = {
        "armed": "lock",
        "arming": "lock",
        "disarmed": "unlock",
        "pending": "warning",
        "triggered": "warning",
        "rearming": "lock",
        "silenced": "bell_off",
    }.get(key, "shield")
    return icon(name, color, size)


def app_pixmap(size: int) -> QPixmap:
    """Baldosa azul con el escudo blanco, al tamano pedido.

    Se separa de `app_icon` para que el empaquetado pueda generar un `.ico` de
    256 px sin duplicar el dibujo.
    """
    from PyQt6.QtGui import QColor, QPainterPath

    tile = QPixmap(size, size)
    tile.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tile)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    radius = size * 0.22
    path.addRoundedRect(0, 0, size, size, radius, radius)
    painter.fillPath(path, QColor("#0f6cbd"))
    inner = int(size * 0.72)
    glyph = pixmap("shield", "#ffffff", inner, stroke=1.9)
    offset = int((size - inner) / 2)
    painter.drawPixmap(offset, offset, glyph)
    painter.end()
    return tile


def app_icon() -> QIcon:
    """Icono de la aplicacion: escudo blanco sobre una baldosa azul."""
    result = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        result.addPixmap(app_pixmap(size))
    return result
