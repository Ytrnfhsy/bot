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
    "settings": "#8A94A6",
    "hide": "#6E8BB5",
    "show": "#6E8BB5",
    "quit": "#E5484D",
}
KINDS = tuple(COLORS)


def menu_icon(kind: str, size: int = 32) -> QIcon:
    icon = QIcon()
    for px in (16, 24, size, size * 2):
        icon.addPixmap(_draw(kind, px))
    return icon


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
    elif kind == "quit":  # кнопка живлення
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(4.5, 5, 15, 15), 125 * 16, 290 * 16)
        p.drawLine(QPointF(12, 3), QPointF(12, 11.5))
    p.end()
    return pm
