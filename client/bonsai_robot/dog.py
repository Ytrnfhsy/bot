"""Таксик анфас — другий персонаж. Уся механіка (стани, гладження, перетягування, кліки) — від RobotWidget,
тут лише інше малювання й «фізика» вух, хвоста та лапок.
"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient

from .robot import RobotWidget, _lerp, _pen

FUR = QColor("#C9773F")
FUR_D = QColor("#9A5226")
FUR_L = QColor("#F2BC86")
NOSE = QColor("#2B1D1A")
OUTLINE = QColor("#5A2E14")
EYE = QColor("#1E1414")
LYING = {"sleeping", "offline"}


def _grey(c: QColor, on: bool) -> QColor:
    if not on:
        return QColor(c)
    v = int(0.3 * c.red() + 0.59 * c.green() + 0.11 * c.blue())
    v = int(v * 0.8 + 45)
    return QColor(v, v, v)


class DogWidget(RobotWidget):
    """Милий таксик, що сидить і дивиться на користувача."""

    PURR_TEXT = "ууу~"

    def __init__(self, scale: float = 1.0, parent=None) -> None:
        super().__init__(scale, parent)
        # [кут, швидкість] вуха; кут > 0 — кінчик відходить назовні (радість, увага), < 0 — притиснуте (сум)
        self._ears = [[4.0, 0.0], [4.0, 0.0]]
        self._prev_bob = 0.0
        self._prev_bob_v = 0.0
        self._dirt: list[list[float]] = []  # [x, y, vx, vy, вік]
        self._next_dirt = 0.0
        self._tail_phase = 0.0
        self._ear_kick = False

    def poke(self) -> None:
        super().poke()
        self._ear_kick = True

    # ---------------- поза для поточного кадру ----------------
    def _look(self) -> tuple[str, str]:
        """(стан тіла, стан мордочки) з урахуванням гладження й перетягування файлу."""
        if self._drop_hover:
            return "happy", "drop"
        if self.purring:
            return "idle", "purr"
        return self.state, self.state

    def _bob(self, st: str) -> float:
        t = self.t
        if st == "happy":
            return -abs(math.sin(t * 5.0)) * 12  # підстрибує
        if st in LYING:
            return 0.0
        return math.sin(t * {"working": 7.0, "thinking": 2.5, "talking": 3.0}.get(st, 2.0)) * 1.6

    def _ear_target(self, st: str, face: str, side: int) -> float:
        if face == "drop":
            return 22
        if face == "purr":
            return 14 + 4 * math.sin(self.t * 5 + side)
        if st == "thinking":
            return 45 if side > 0 else 2  # одне вушко вгору
        return {"happy": 26, "sad": -14, "sleeping": 50, "offline": 50, "waiting": 8, "talking": 8,
                "working": 0}.get(st, 4)

    def _tick(self) -> None:
        super()._tick()
        dt = 1 / 60
        st, face = self._look()
        # хвіст: частота виляння залежить від настрою
        freq = {"happy": 14, "talking": 6, "working": 8, "waiting": 5, "thinking": 1.5,
                "sad": 1.0, "sleeping": 0.6, "offline": 0.0}.get(st, 3.0)
        if face in ("purr", "drop"):
            freq = 15
        self._tail_phase += dt * freq
        # вуха: пружина + інерція від руху голови (стрибнув — вуха злітають)
        bob = self._bob(st)
        v = (bob - self._prev_bob) / dt
        acc = (v - self._prev_bob_v) / dt
        self._prev_bob, self._prev_bob_v = bob, v
        for i, side in enumerate((-1, 1)):
            ang, vel = self._ears[i]
            target = self._ear_target(st, face, side)
            force = 55 * (target - ang) - 7 * vel + max(-4000.0, min(4000.0, acc)) * 0.06
            if self._ear_kick:  # клік — вушка підскакують
                vel += 160
            vel += force * dt
            ang = max(-30.0, min(70.0, ang + vel * dt))
            self._ears[i] = [ang, vel]
        self._ear_kick = False
        # грудочки землі, коли копає
        now = self.t
        if self.state == "working" and not self._drop_hover and now >= self._next_dirt:
            self._next_dirt = now + random.uniform(0.06, 0.14)
            self._dirt.append([62.0, 196.0, random.uniform(-90, -30), random.uniform(-170, -90), 0.0])
        for d in self._dirt:
            d[0] += d[2] * dt
            d[1] += d[3] * dt
            d[3] += 420 * dt
            d[4] += dt
        self._dirt = [d for d in self._dirt if d[4] < 0.9 and d[1] < 215]

    # ---------------- малювання ----------------
    def paintEvent(self, _event) -> None:  # noqa: C901
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self.scale_factor, self.scale_factor)
        t = self.t
        st, face = self._look()
        grey = st == "offline"
        lie = st in LYING
        bob = self._bob(st)

        # тінь
        w = 70 + (bob * 1.5 if st == "happy" else 0)
        sg = QRadialGradient(QPointF(100, 207), w)
        sg.setColorAt(0, QColor(0, 0, 0, 65))
        sg.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(sg)
        p.drawEllipse(QPointF(100, 207), w, 9)

        p.save()
        squish = self._squish * 0.12
        p.translate(100, 205 + bob)
        p.scale(1 + squish, 1 - squish)
        p.translate(-100, -205)
        out = _pen(_grey(OUTLINE, grey), 2.2)

        # хвіст
        wag = math.sin(self._tail_phase) * (22 if face in ("purr", "drop") or st == "happy" else 14)
        base = {"sad": 50, "sleeping": 40, "offline": 50}.get(st, -12)
        self._draw_tail(p, out, grey, base + wag)
        if lie:
            self._draw_lying_body(p, out, grey, t)
        else:
            self._draw_sitting_body(p, out, grey, st, face, t)

        # голова
        p.save()
        tilt = {"thinking": -12 + 2 * math.sin(t * 2), "sad": 4, "sleeping": 3, "offline": 0}.get(
            st, math.sin(t * 1.1) * 2)
        head_y = {"working": 8, "sad": 6}.get(st, 0)
        if face == "purr":
            tilt = _lerp(tilt, 8 + math.sin(t * 5) * 6, self._purr)
        if face == "drop":
            tilt, head_y = -4, -2
        if lie:
            head_y = 98 + math.sin(t * 1.2) * 1.0
        p.translate(100, 76 + head_y)
        p.rotate(tilt)
        self._draw_head(p, out, grey, st, face, t, lie)
        p.restore()
        p.restore()

        self._draw_fx(p, st, face, t, bob)
        self._draw_purr(p, t, bob)
        p.end()

    def _fur(self, rect: QRectF, grey: bool, base: QColor = FUR) -> QLinearGradient:
        g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        g.setColorAt(0, _grey(base.lighter(114), grey))
        g.setColorAt(1, _grey(base.darker(110), grey))
        return g

    def _draw_tail(self, p: QPainter, out: QPen, grey: bool, angle: float) -> None:
        p.save()
        p.translate(138, 178)
        p.rotate(angle)
        tail = QPainterPath(QPointF(0, 0))
        tail.cubicTo(18, -6, 30, -26, 30, -50)
        tail.cubicTo(24, -30, 14, -16, -2, -10)
        p.setPen(out)
        p.setBrush(_grey(FUR_D, grey))
        p.drawPath(tail)
        p.restore()

    def _paw(self, p: QPainter, out: QPen, grey: bool, x: float, angle: float, length: float = 34) -> None:
        p.save()
        p.translate(x, 160)
        p.rotate(angle)
        r = QRectF(-8, 0, 16, length)
        p.setPen(out)
        p.setBrush(self._fur(r, grey))
        p.drawRoundedRect(r, 8, 8)
        p.setBrush(_grey(FUR_L, grey))
        p.drawEllipse(QRectF(-10, length - 9, 20, 12))
        if abs(angle) < 5:  # пальчики
            p.setPen(_pen(_grey(QColor("#B07A4E"), grey), 1.3))
            p.drawLine(QPointF(-3, length), QPointF(-3, length + 2))
            p.drawLine(QPointF(3, length), QPointF(3, length + 2))
        p.restore()

    def _draw_sitting_body(self, p: QPainter, out: QPen, grey: bool, st: str, face: str, t: float) -> None:
        # задні лапи-стегна з боків
        for x in (40, 120):
            r = QRectF(x, 158, 42, 40)
            p.setPen(out)
            p.setBrush(self._fur(r, grey))
            p.drawEllipse(r)
        p.setBrush(_grey(FUR_L, grey))
        for x in (44, 132):
            p.drawEllipse(QRectF(x, 190, 26, 13))
        # тулуб із диханням
        breath = 1 + 0.015 * math.sin(t * 2.2)
        p.save()
        p.translate(100, 198)
        p.scale(breath, breath)
        p.translate(-100, -198)
        body = QPainterPath(QPointF(64, 120))
        body.cubicTo(52, 150, 56, 196, 100, 198)
        body.cubicTo(144, 196, 148, 150, 136, 120)
        body.closeSubpath()
        p.setPen(out)
        p.setBrush(self._fur(QRectF(55, 120, 90, 80), grey))
        p.drawPath(body)
        chest = QPainterPath(QPointF(84, 128))
        chest.cubicTo(76, 150, 84, 176, 100, 180)
        chest.cubicTo(116, 176, 124, 150, 116, 128)
        chest.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_grey(FUR_L, grey))
        p.drawPath(chest)
        p.restore()
        # передні лапки: копає / піднімає / «дає лапу» при наведенні
        left = right = 0.0
        # кут > 0 відводить ліву лапку вбік-угору, < 0 — праву
        if st == "working" and face not in ("drop", "purr"):
            left = 40 + 28 * math.sin(t * 14)  # гребе землю
        elif st == "waiting":
            right = -150 + 12 * math.sin(t * 6)  # тягне лапку: «дозволиш?»
        elif self._wave > 0.05:
            right = -self._wave * (125 + 15 * math.sin(t * 9))  # «дай п'ять»
        self._paw(p, out, grey, 86, left, 30 if left else 34)
        self._paw(p, out, grey, 114, right, 30 if right else 34)

    def _draw_lying_body(self, p: QPainter, out: QPen, grey: bool, t: float) -> None:
        breath = 1 + 0.03 * math.sin(t * 1.2)
        back = QRectF(38, 140 - 4 * (breath - 1) * 30, 124, 52 * breath)
        p.setPen(out)
        p.setBrush(self._fur(back, grey))
        p.drawRoundedRect(back, 26, 26)
        for x in (58, 116):  # передні лапки під мордочкою
            p.setBrush(self._fur(QRectF(x, 182, 26, 20), grey))
            p.drawRoundedRect(QRectF(x, 182, 26, 22), 10, 10)
            p.setBrush(_grey(FUR_L, grey))
            p.drawEllipse(QRectF(x + 1, 194, 24, 12))

    def _draw_ear(self, p: QPainter, out: QPen, grey: bool, side: int, angle: float) -> None:
        p.save()
        p.translate(side * 42, -24)
        p.rotate(-side * angle)
        e = QPainterPath(QPointF(0, 0))
        e.cubicTo(side * 20, 6, side * 22, 52, side * 8, 66)
        e.cubicTo(side * -2, 74, side * -16, 62, side * -12, 40)
        e.cubicTo(side * -10, 22, side * -8, 6, 0, 0)
        p.setPen(out)
        p.setBrush(self._fur(QRectF(-20, 0, 40, 72), grey, FUR_D))
        p.drawPath(e)
        p.restore()

    def _draw_head(self, p: QPainter, out: QPen, grey: bool, st: str, face: str, t: float, lie: bool) -> None:
        if not lie:  # нашийник кольору стану
            col = QColor("#B0B6BF") if grey else QColor(self._color)
            p.setPen(_pen(col.darker(130), 2))
            p.setBrush(col)
            p.drawRoundedRect(QRectF(-34, 38, 68, 12), 6, 6)
            p.setBrush(QColor("#D6D6D6") if grey else QColor("#FFD54A"))
            p.drawEllipse(QRectF(-7 + math.sin(t * 2) * 0.8, 45, 14, 14))
        # череп
        head = QPainterPath(QPointF(0, -46))
        head.cubicTo(36, -46, 50, -24, 48, 0)
        head.cubicTo(46, 26, 26, 40, 0, 40)
        head.cubicTo(-26, 40, -46, 26, -48, 0)
        head.cubicTo(-50, -24, -36, -46, 0, -46)
        p.setPen(out)
        p.setBrush(self._fur(QRectF(-48, -46, 96, 86), grey))
        p.drawPath(head)
        # світлі «брівки» — піднімаються від здивування, опускаються від суму
        brow = {"thinking": -3, "sad": 2, "waiting": -3}.get(st, 0)
        if face == "drop":
            brow = -4
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_grey(FUR_L, grey))
        p.drawEllipse(QRectF(-30, -34 + brow, 12, 7))
        p.drawEllipse(QRectF(18, -34 + brow, 12, 7))
        # мордочка
        muz = QPainterPath(QPointF(0, -12))
        muz.cubicTo(20, -12, 28, 6, 26, 20)
        muz.cubicTo(24, 34, 12, 40, 0, 40)
        muz.cubicTo(-12, 40, -24, 34, -26, 20)
        muz.cubicTo(-28, 6, -20, -12, 0, -12)
        p.setPen(out)
        p.setBrush(_grey(FUR_L, grey))
        p.drawPath(muz)
        self._draw_eyes(p, grey, st, face, t)
        if not grey:  # щічки
            for cx in (-30, 30):
                bl = QRadialGradient(QPointF(cx, 8), 10)
                a = 150 + (80 if face in ("purr", "drop") or st == "happy" else 0)
                bl.setColorAt(0, QColor(255, 120, 150, min(255, a)))
                bl.setColorAt(1, QColor(255, 120, 150, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(bl)
                p.drawEllipse(QRectF(cx - 11, -3, 22, 22))
        self._draw_nose_mouth(p, grey, st, face, t)
        for i, side in enumerate((-1, 1)):
            self._draw_ear(p, out, grey, side, self._ears[i][0])
        if st == "sad" and face == "sad":  # сльозинка стікає по щоці
            k = (t * 0.6) % 1.0
            col = QColor("#7CC4FF")
            col.setAlphaF(1 - k * 0.8)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            y = -4 + k * 22
            drop = QPainterPath(QPointF(-26, y - 8))
            drop.cubicTo(-33, y + 4, -19, y + 4, -26, y - 8)
            p.drawPath(drop)

    def _draw_eyes(self, p: QPainter, grey: bool, st: str, face: str, t: float) -> None:
        eye = _grey(EYE, grey)
        kind = {"happy": "happy", "sad": "sad", "sleeping": "closed", "offline": "dots",
                "waiting": "big", "working": "down"}.get(st, "open")
        if face in ("purr",):
            kind = "happy"
        elif face == "drop":
            kind = "big"
        gx, gy = self._gaze.x(), self._gaze.y()
        if face == "drop":
            gx, gy = 0, -1
        for ex in (-22, 22):
            ey = -14
            if kind in ("open", "big", "down"):
                r = 10 if kind == "big" else 8.5
                dx, dy = (0, 3) if kind == "down" else (gx * 3.5, gy * 3)
                open_k = max(0.08, 1 - self._blink)
                if open_k < 0.3:  # моргнув
                    p.setPen(_pen(eye, 2.6))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    m = QPainterPath(QPointF(ex - 7, ey))
                    m.quadTo(ex, ey + 4, ex + 7, ey)
                    p.drawPath(m)
                    continue
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(eye)
                p.drawEllipse(QPointF(ex + dx, ey + dy), r * 0.9, r * open_k)
                p.setBrush(QColor("white"))
                p.drawEllipse(QPointF(ex + dx + 3, ey + dy - 3.5 * open_k), 3.2, 3.2 * open_k)
                p.drawEllipse(QPointF(ex + dx - 2.5, ey + dy + 3 * open_k), 1.4, 1.4 * open_k)
                if kind == "big":  # «щенячі» очі блищать
                    p.drawEllipse(QPointF(ex + dx - 2.5, ey + dy - 1), 2 + 0.4 * math.sin(t * 5), 2)
            elif kind in ("happy", "closed"):
                p.setPen(_pen(eye, 2.8))
                p.setBrush(Qt.BrushStyle.NoBrush)
                up = kind == "happy"
                m = QPainterPath(QPointF(ex - 7, ey + (2 if up else -1)))
                m.quadTo(ex, ey + (-8 if up else 6), ex + 7, ey + (2 if up else -1))
                p.drawPath(m)
            elif kind == "sad":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(eye)
                p.drawEllipse(QPointF(ex, ey + 2), 7, 7.5 * max(0.1, 1 - self._blink))
                p.setBrush(QColor("white"))
                p.drawEllipse(QPointF(ex + 2.5, ey - 1), 2.6, 2.6)
                p.setPen(_pen(_grey(OUTLINE, grey), 2.2))
                s = 1 if ex < 0 else -1
                p.drawLine(QPointF(ex - 8 * s, ey - 8), QPointF(ex + 7 * s, ey - 12))
            elif kind == "dots":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(eye)
                p.drawEllipse(QPointF(ex, ey), 3, 3)

    def _draw_nose_mouth(self, p: QPainter, grey: bool, st: str, face: str, t: float) -> None:
        # носик (винюхує — ворушиться)
        sniff = 1 + (0.12 * abs(math.sin(t * 16)) if st == "working" else 0)
        p.save()
        p.translate(0, 6)
        p.scale(sniff, sniff)
        nose = QPainterPath(QPointF(-10, -2))
        nose.cubicTo(-10, -8, 10, -8, 10, -2)
        nose.cubicTo(10, 4, 3, 8, 0, 8)
        nose.cubicTo(-3, 8, -10, 4, -10, -2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_grey(NOSE, grey))
        p.drawPath(nose)
        p.setBrush(QColor(255, 255, 255, 170))
        p.drawEllipse(QRectF(-5, -6, 6, 3.5))
        p.restore()
        mouth = {"talking": "talk", "happy": "tongue", "sad": "frown", "thinking": "flat",
                 "offline": "flat", "working": "sniff"}.get(st, "smile")
        if face in ("purr", "drop"):
            mouth = "tongue"
        ink = _grey(QColor("#4A2615"), grey)
        p.setPen(_pen(ink, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(0, 14), QPointF(0, 19))
        if mouth == "smile":
            m = QPainterPath(QPointF(-10, 19))
            m.quadTo(-5, 25, 0, 19)
            m.quadTo(5, 25, 10, 19)
            p.drawPath(m)
        elif mouth in ("talk", "tongue"):
            if mouth == "talk":
                depth = 21 + 12 * abs(math.sin(t * 9)) * (0.6 + 0.4 * math.sin(t * 2.3))
            else:
                depth = 29
            p.setBrush(_grey(QColor("#7A2E2E"), grey))
            o = QPainterPath(QPointF(-10, 19))
            o.quadTo(0, 17, 10, 19)
            o.quadTo(9, depth, 0, depth)
            o.quadTo(-9, depth, -10, 19)
            p.drawPath(o)
            if mouth == "tongue":  # язичок, дихає «пф-пф»
                ln = 15 + 2.5 * math.sin(t * 9)
                p.setBrush(_grey(QColor("#FF7F9A"), grey))
                p.setPen(_pen(_grey(QColor("#C8506A"), grey), 1.5))
                p.drawRoundedRect(QRectF(-6, 24, 12, ln), 6, 6)
                p.drawLine(QPointF(0, 27), QPointF(0, 24 + ln * 0.55))
        elif mouth == "frown":
            m = QPainterPath(QPointF(-9, 25))
            m.quadTo(0, 18, 9, 25)
            p.drawPath(m)
        elif mouth == "flat":
            p.drawLine(QPointF(-7, 22), QPointF(7, 22))
        elif mouth == "sniff":
            m = QPainterPath(QPointF(-5, 21))
            m.quadTo(0, 18, 5, 21)
            p.drawPath(m)
        if st == "sleeping" and face == "sleeping":  # бульбашка з носика
            r = 3 + 4 * (0.5 + 0.5 * math.sin(t * 1.2))
            p.setPen(_pen(QColor(150, 190, 255, 200), 1.2))
            p.setBrush(QColor(200, 225, 255, 120))
            p.drawEllipse(QPointF(12 + r * 0.6, 4), r, r)

    def _draw_fx(self, p: QPainter, st: str, face: str, t: float, bob: float) -> None:
        if face == "drop":  # чекає «кісточку»
            p.save()
            p.translate(100, 12 + 3 * math.sin(t * 8))
            p.rotate(-15 + 8 * math.sin(t * 4))
            p.setPen(_pen("#C9B79C", 2))
            p.setBrush(QColor("#FFF6E4"))
            p.drawRoundedRect(QRectF(-15, -4, 30, 8), 3, 3)
            for dx in (-15, 15):
                for dy in (-4.5, 4.5):
                    p.drawEllipse(QPointF(dx, dy), 5, 5)
            p.restore()
            return
        if face == "purr":
            return
        if st == "thinking":
            f = QFont("Sans", 18, QFont.Weight.Black)
            p.setFont(f)
            c = QColor("#8B6FE8")
            c.setAlphaF(0.75 + 0.25 * math.sin(t * 3))
            p.setPen(c)
            p.drawText(QPointF(150, 30 + 3 * math.sin(t * 2.5)), "?")
        elif st == "waiting":  # «дозволиш?» — бульбашка зі знаком оклику
            rect = QRectF(150, 0 + bob, 40, 36)
            p.setPen(QPen(QColor("#5B6B82"), 2))
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawRoundedRect(rect, 14, 14)
            p.setFont(QFont("Sans", 18, QFont.Weight.Black))
            c = QColor(self._color)
            c.setAlphaF(0.8 + 0.2 * math.sin(t * 6))
            p.setPen(c)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "!")
        elif st == "working":
            p.setPen(Qt.PenStyle.NoPen)
            for x, y, _vx, _vy, age in self._dirt:
                c = QColor("#9C7A5B")
                c.setAlphaF(max(0.0, 1 - age / 0.9))
                p.setBrush(c)
                p.drawEllipse(QPointF(x, y), 3, 3)
            p.setPen(_pen("#B98A66", 2))  # «нюх-нюх»
            k = abs(math.sin(t * 8))
            p.drawLine(QPointF(128, 96 + bob), QPointF(134 + 3 * k, 92 + bob))
            p.drawLine(QPointF(130, 104 + bob), QPointF(137 + 3 * k, 104 + bob))
        elif st == "sleeping":
            for i in range(3):
                phase = (t * 0.5 + i / 3) % 1.0
                p.setFont(QFont("Sans", int(9 + 7 * phase), QFont.Weight.Bold))
                c = QColor("#7C83E8")
                c.setAlphaF(1 - phase)
                p.setPen(c)
                p.drawText(QPointF(146 + phase * 26 + i * 2, 130 - phase * 40), "z")
        elif st == "offline":
            p.setFont(QFont("Sans", 14, QFont.Weight.Bold))
            p.setPen(QColor("#9CA3AF"))
            p.drawText(QRectF(140, 96, 60, 30), Qt.AlignmentFlag.AlignCenter, "…")
        elif st == "happy":
            for i in range(4):
                a = i * math.pi / 2 + t
                r = 86 + 6 * math.sin(t * 5 + i)
                pos = QPointF(100 + math.cos(a) * r, 100 + bob + math.sin(a) * 50)
                self._sparkle(p, pos, 4 + 2 * math.sin(t * 6 + i), QColor("#FACC15"))
