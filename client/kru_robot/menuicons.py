"""Кольорові векторні іконки для меню персонажа й трею.

Емодзі в пунктах меню ненадійні: меню трею KDE малює сам і емодзі відкидає, а без кольорового
шрифту вони виходять порожніми квадратиками. Ці іконки малюються QPainter-ом, тож однакові всюди.
Кольори середньої яскравості — видно і на світлій, і на темній темі.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QTransform

COLORS = {
    "chat": "#3D8BFD",
    "new": "#2FB36D",
    "chats": "#E9A23B",
    "rename": "#9B6BF2",
    "export": "#14A3A3",
    "stop": "#E5484D",
    "think": "#9B6BF2",
    "settings": "#8A94A6",
    "hide": "#6E8BB5",
    "show": "#6E8BB5",
    "quit": "#E5484D",
    # позначки в стрічці чату
    "user": "#3D8BFD",
    "tool": "#8A94A6",
    "memory": "#E9A23B",
    "delegate": "#14A3A3",
    "file": "#3D8BFD",
    "plan": "#2FB36D",
    "clock": "#E9A23B",
    "retry": "#3D8BFD",
    "warn": "#E5484D",
    "ok": "#2FB36D",
    "download": "#3D8BFD",
    "search": "#3D8BFD",
    "send": "#14A3A3",
    "link": "#E5484D",
    "mic": "#E2558E",
    "speaker": "#14A3A3",
}
KINDS = tuple(COLORS)


def menu_icon(kind: str, size: int = 32) -> QIcon:
    icon = QIcon()
    for px in (16, 24, size, size * 2):
        icon.addPixmap(_draw(kind, px))
    return icon


def feed_image(kind: str, size: int = 15, ratio: float = 2.0):  # noqa: D401
    """Іконка для стрічки чату (QImage з потрібною щільністю пікселів — чітка на HiDPI)."""
    img = _draw(kind, int(size * ratio)).toImage()
    img.setDevicePixelRatio(ratio)
    return img


_FILES: dict[str, str] = {}


def icon_file(kind: str) -> str:
    """Шлях до PNG іконки (для QLabel із rich text: там картинки — лише з файлів чи ресурсів)."""
    if kind not in _FILES:
        import os
        import tempfile

        folder = os.path.join(tempfile.gettempdir(), f"kru-icons-{os.getuid() if hasattr(os, 'getuid') else 0}")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{kind}.png")
        _draw(kind, 30).save(path)
        _FILES[kind] = path
    return _FILES[kind]


def _ellipse(x: float, y: float, rx: float, ry: float) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(QPointF(x, y), rx, ry)
    return path


def _draw(kind: str, px: int) -> QPixmap:
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(px / 24, px / 24)  # координати 24×24
    color = QColor(COLORS.get(kind, "#8A94A6"))
    pen = QPen(color, 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    fill = QColor(color)
    fill.setAlphaF(0.22)
    p.setBrush(fill)

    if kind == "chat":  # бульбашка з крапками
        path = QPainterPath()
        path.addRoundedRect(QRectF(3, 4, 18, 13), 4, 4)
        tail = QPainterPath()
        tail.moveTo(7, 16)
        tail.lineTo(6, 21)
        tail.lineTo(12, 16.5)
        p.drawPath(path.united(tail))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        for x in (8, 12, 16):
            p.drawEllipse(QPointF(x, 10.5), 1.3, 1.3)
    elif kind == "new":  # аркуш і плюс
        p.drawRoundedRect(QRectF(4.5, 3, 15, 18), 2.5, 2.5)
        p.drawLine(QPointF(12, 8), QPointF(12, 16))
        p.drawLine(QPointF(8, 12), QPointF(16, 12))
    elif kind == "chats":  # тека
        path = QPainterPath()
        path.moveTo(3, 6.5)
        path.quadTo(3, 5, 4.5, 5)
        path.lineTo(9.5, 5)
        path.lineTo(11.5, 7.5)
        path.lineTo(19.5, 7.5)
        path.quadTo(21, 7.5, 21, 9)
        path.lineTo(21, 18.5)
        path.quadTo(21, 20, 19.5, 20)
        path.lineTo(4.5, 20)
        path.quadTo(3, 20, 3, 18.5)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(QPointF(3, 11), QPointF(21, 11))
    elif kind == "rename":  # олівець
        pencil = QPolygonF([QPointF(16, 3.5), QPointF(20.5, 8), QPointF(9, 19.5), QPointF(3.5, 20.5),
                            QPointF(4.5, 15)])
        p.drawPolygon(pencil)
        p.drawLine(QPointF(13.5, 6), QPointF(18, 10.5))
    elif kind == "export":  # стрілка з лотка
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(QPolygonF([QPointF(4, 13), QPointF(4, 19), QPointF(20, 19), QPointF(20, 13)]))
        p.drawLine(QPointF(12, 4), QPointF(12, 15))
        p.drawPolyline(QPolygonF([QPointF(7.5, 8.5), QPointF(12, 4), QPointF(16.5, 8.5)]))
    elif kind == "stop":  # квадрат
        p.setBrush(color)
        p.drawRoundedRect(QRectF(5.5, 5.5, 13, 13), 2.5, 2.5)
    elif kind == "settings":  # шестерня
        gear = QPainterPath()
        gear.addEllipse(QPointF(12, 12), 6.2, 6.2)
        for i in range(8):
            a = i * math.pi / 4
            tooth = QPainterPath()
            tooth.addRoundedRect(QRectF(-1.8, -9.3, 3.6, 4.5), 0.8, 0.8)
            gear = gear.united(QTransform().translate(12, 12).rotateRadians(a).map(tooth))
        p.drawPath(gear)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(12, 12), 2.6, 2.6)
    elif kind in {"hide", "show"}:  # око (закреслене — сховати)
        eye = QPainterPath()
        eye.moveTo(2.5, 12)
        eye.quadTo(12, 2.5, 21.5, 12)
        eye.quadTo(12, 21.5, 2.5, 12)
        p.drawPath(eye)
        p.setBrush(color)
        p.drawEllipse(QPointF(12, 12), 2.8, 2.8)
        if kind == "hide":
            p.drawLine(QPointF(4, 20), QPointF(20, 4))
    elif kind == "think":  # хмаринка думок
        cloud = QPainterPath()
        cloud.addEllipse(QPointF(9, 10), 5, 4.5)
        cloud = cloud.united(_ellipse(14.5, 8.5, 5, 4.5)).united(_ellipse(15.5, 13, 5, 3.8)).united(
            _ellipse(9.5, 14, 5, 3.6))
        p.drawPath(cloud)
        p.setBrush(color)
        p.drawEllipse(QPointF(6.5, 19.5), 1.4, 1.4)
        p.drawEllipse(QPointF(4, 22), 0.9, 0.9)
    elif kind == "user":  # людина
        p.drawEllipse(QPointF(12, 8), 4, 4)
        body = QPainterPath()
        body.moveTo(4.5, 21)
        body.cubicTo(4.5, 15, 8, 13.5, 12, 13.5)
        body.cubicTo(16, 13.5, 19.5, 15, 19.5, 21)
        body.closeSubpath()
        p.drawPath(body)
    elif kind == "tool":  # гайковий ключ
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(5, 19), QPointF(13, 11))
        head = QPainterPath()
        head.addEllipse(QPointF(15.5, 8.5), 4.6, 4.6)
        p.setBrush(fill)
        p.drawPath(head)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 0))
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.drawRect(QRectF(16.2, 3, 4.5, 4.5))  # проріз ключа
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    elif kind == "memory":  # лампочка
        bulb = QPainterPath()
        bulb.moveTo(9, 15.5)
        bulb.cubicTo(5, 12.5, 6, 4, 12, 4)
        bulb.cubicTo(18, 4, 19, 12.5, 15, 15.5)
        bulb.lineTo(15, 17)
        bulb.lineTo(9, 17)
        bulb.closeSubpath()
        p.drawPath(bulb)
        p.drawLine(QPointF(9.5, 20), QPointF(14.5, 20))
    elif kind == "delegate":  # дві людини
        for x, r in ((8.5, 3.2), (16, 3.2)):
            p.drawEllipse(QPointF(x, 8), r, r)
        for x in (8.5, 16):
            body = QPainterPath()
            body.moveTo(x - 5, 20)
            body.cubicTo(x - 5, 15, x - 2.5, 13.5, x, 13.5)
            body.cubicTo(x + 2.5, 13.5, x + 5, 15, x + 5, 20)
            body.closeSubpath()
            p.drawPath(body)
    elif kind == "file":  # скріпка
        p.setBrush(Qt.BrushStyle.NoBrush)
        clip = QPainterPath()
        clip.moveTo(15.5, 7)
        clip.lineTo(8.5, 14)
        clip.cubicTo(6.5, 16, 9, 18.5, 11, 16.5)
        clip.lineTo(18.5, 9)
        clip.cubicTo(21.5, 6, 17, 1.5, 14, 4.5)
        clip.lineTo(6, 12.5)
        clip.cubicTo(1.5, 17, 8, 23.5, 12.5, 19)
        clip.lineTo(18, 13.5)
        p.drawPath(clip)
    elif kind == "plan":  # список із галочкою
        p.drawRoundedRect(QRectF(4, 3, 16, 18), 3, 3)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(QPolygonF([QPointF(7.5, 9), QPointF(9, 10.5), QPointF(11.5, 7.5)]))
        p.drawLine(QPointF(13.5, 9), QPointF(17, 9))
        p.drawLine(QPointF(7.5, 15), QPointF(17, 15))
    elif kind == "clock":  # годинник
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        p.drawPolyline(QPolygonF([QPointF(12, 7), QPointF(12, 12), QPointF(15.5, 14)]))
    elif kind == "retry":  # кругова стрілка
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(4.5, 4.5, 15, 15), 60 * 16, 290 * 16)
        p.setBrush(color)
        p.drawPolygon(QPolygonF([QPointF(16, 2.5), QPointF(17.5, 8.5), QPointF(11.5, 7.5)]))
    elif kind == "warn":  # трикутник зі знаком оклику
        p.drawPolygon(QPolygonF([QPointF(12, 3.5), QPointF(21, 20), QPointF(3, 20)]))
        p.drawLine(QPointF(12, 9.5), QPointF(12, 14))
        p.setBrush(color)
        p.drawEllipse(QPointF(12, 17), 0.9, 0.9)
    elif kind == "ok":  # галочка в колі
        p.drawEllipse(QPointF(12, 12), 8.5, 8.5)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(QPolygonF([QPointF(8, 12.5), QPointF(11, 15.5), QPointF(16.5, 9)]))
    elif kind == "download":  # стрілка вниз у лоток
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolyline(QPolygonF([QPointF(4, 14), QPointF(4, 20), QPointF(20, 20), QPointF(20, 14)]))
        p.drawLine(QPointF(12, 3.5), QPointF(12, 15))
        p.drawPolyline(QPolygonF([QPointF(7.5, 10.5), QPointF(12, 15), QPointF(16.5, 10.5)]))
    elif kind == "search":  # лупа
        p.drawEllipse(QPointF(10.5, 10.5), 6, 6)
        p.drawLine(QPointF(15, 15), QPointF(20.5, 20.5))
    elif kind == "send":  # паперовий літачок
        p.drawPolygon(QPolygonF([QPointF(3, 11), QPointF(21, 3.5), QPointF(14, 21), QPointF(11, 13.5)]))
        p.drawLine(QPointF(11, 13.5), QPointF(21, 3.5))
    elif kind == "link":  # обірваний зв'язок: антена
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(4, 4, 16, 16), 30 * 16, 120 * 16)
        p.drawArc(QRectF(8, 8, 8, 8), 30 * 16, 120 * 16)
        p.setBrush(color)
        p.drawEllipse(QPointF(12, 15), 1.6, 1.6)
        p.drawLine(QPointF(5, 21), QPointF(19, 21))
    elif kind == "mic":  # мікрофон
        p.drawRoundedRect(QRectF(8.5, 2.5, 7, 12), 3.5, 3.5)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(5, 5.5, 14, 12.5), 200 * 16, 140 * 16)
        p.drawLine(QPointF(12, 18), QPointF(12, 21))
        p.drawLine(QPointF(8.5, 21), QPointF(15.5, 21))
    elif kind == "speaker":  # гучномовець із хвилями
        p.drawPolygon(QPolygonF([QPointF(3.5, 9), QPointF(7.5, 9), QPointF(12.5, 4.5), QPointF(12.5, 19.5),
                                 QPointF(7.5, 15), QPointF(3.5, 15)]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(10, 8, 7, 8), -50 * 16, 100 * 16)
        p.drawArc(QRectF(10, 4.5, 11, 15), -50 * 16, 100 * 16)
    elif kind == "quit":  # кнопка живлення
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(4.5, 5, 15, 15), 125 * 16, 290 * 16)
        p.drawLine(QPointF(12, 3), QPointF(12, 11.5))
    p.end()
    return pm
