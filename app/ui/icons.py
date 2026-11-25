"""Small, DPI-aware line icons drawn locally; no font or network dependency."""
from PySide6.QtCore import QPointF, QRectF, Qt, QSize
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF


def icon(name: str, color: str = "#9ba9bf", size: int = 20) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 24, size / 24)
    p.setPen(QPen(QColor(color), 1.7, Qt.PenStyle.SolidLine,
                 Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))

    def line(x1, y1, x2, y2):
        p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    if name == "grid":
        for x, y in ((4, 4), (14, 4), (4, 14), (14, 14)):
            p.drawRoundedRect(QRectF(x, y, 6, 6), 1, 1)
    elif name == "folder":
        p.drawPolygon(QPolygonF([QPointF(3, 6), QPointF(9, 6), QPointF(11, 9),
                                QPointF(21, 9), QPointF(21, 20), QPointF(3, 20)]))
    elif name == 'sound':
        for x,h in [(4,5),(8,10),(12,17),(16,10),(20,5)]:line(x,12-h/2,x,12+h/2)
    elif name == 'download':
        line(12, 3, 12, 15); line(7, 11, 12, 16); line(12, 16, 17, 11)
        line(4, 16, 4, 21); line(4, 21, 20, 21); line(20, 21, 20, 16)
    elif name == "library":
        p.drawRoundedRect(QRectF(3, 4, 6, 16), 1, 1)
        p.drawRoundedRect(QRectF(12, 4, 6, 16), 1, 1)
        line(20, 6, 22, 20)
    elif name == "search":
        p.drawEllipse(QRectF(4, 4, 12, 12)); line(14, 14, 20, 20)
    elif name == "refresh":
        p.drawArc(QRectF(4, 4, 16, 16), 45 * 16, 290 * 16)
        line(19, 3, 19, 8); line(19, 8, 14, 8)
    elif name == "history":
        p.drawEllipse(QRectF(4, 4, 16, 16)); line(12, 7, 12, 12); line(12, 12, 16, 14)
    elif name == "settings":
        for y, x in ((6, 9), (12, 16), (18, 7)):
            line(3, y, x - 2, y); line(x + 2, y, 21, y)
            p.drawEllipse(QRectF(x - 2, y - 2, 4, 4))
    elif name == "agent":
        p.drawRoundedRect(QRectF(4, 7, 16, 13), 4, 4)
        line(12, 3, 12, 7); line(8, 12, 8, 14); line(16, 12, 16, 14); line(9, 17, 15, 17)
    elif name == "plus":
        line(12, 5, 12, 19); line(5, 12, 19, 12)
    elif name == "edit":
        p.drawPolygon(QPolygonF([QPointF(5, 15), QPointF(15, 5), QPointF(19, 9), QPointF(9, 19), QPointF(4, 20)]))
        line(13, 7, 17, 11)
    elif name == "trash":
        line(4, 6, 20, 6); line(9, 3, 15, 3)
        p.drawRoundedRect(QRectF(6, 6, 12, 15), 2, 2)
        line(10, 10, 10, 17); line(14, 10, 14, 17)
    elif name == "copy":
        p.drawRoundedRect(QRectF(8, 8, 12, 13), 2, 2)
        line(16, 5, 16, 3); line(16, 3, 4, 3); line(4, 3, 4, 16)
    elif name == "stop":
        p.drawRoundedRect(QRectF(6, 6, 12, 12), 2, 2)
    elif name == "pause":
        line(8, 5, 8, 19); line(16, 5, 16, 19)
    elif name == "play":
        p.drawPolygon(QPolygonF([QPointF(7, 4), QPointF(20, 12), QPointF(7, 20)]))
    elif name == "globe":
        p.drawEllipse(QRectF(3, 3, 18, 18)); p.drawEllipse(QRectF(8, 3, 8, 18)); line(3, 12, 21, 12)
    elif name == "arrow":
        line(5, 12, 19, 12); line(14, 7, 19, 12); line(14, 17, 19, 12)
    elif name == "send":
        line(12, 19, 12, 5); line(6, 11, 12, 5); line(18, 11, 12, 5)
    elif name == "close":
        line(6, 6, 18, 18); line(6, 18, 18, 6)
    p.end()
    return QIcon(pm)


def decorate(button, name, color="#9ba9bf"):
    button.setProperty("lineIcon", name)
    button.setIcon(icon(name, color))
    button.setIconSize(QSize(18, 18))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
