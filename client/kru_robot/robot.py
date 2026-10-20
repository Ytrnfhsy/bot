"""Анімований робот, намальований QPainter (без зовнішніх зображень).

Стани: idle, thinking, working, talking, happy, sad, sleeping, waiting, offline.
"""

from __future__ import annotations

import math
import random
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

# Колір «настрою» (очі, антена, підсвітка) для кожного стану
STATE_COLORS = {
    "idle": QColor("#4FD1FF"),
    "thinking": QColor("#B388FF"),
    "working": QColor("#FFC857"),
    "talking": QColor("#4FD1FF"),
    "happy": QColor("#5BE49B"),
    "sad": QColor("#FF6B6B"),
    "sleeping": QColor("#5C7CFA"),
    "waiting": QColor("#FF9F43"),
    "listening": QColor("#FF6FA8"),
    "offline": QColor("#8A94A6"),
}

BASE_W, BASE_H = 200.0, 220.0
HEAD_RECT = QRectF(30, 30, 140, 100)  # де «гладити» (у базових координатах)
PETTABLE = {"idle", "happy", "sleeping"}  # під час роботи гладити не відволікаємо
PURR_COLOR = QColor("#FF7EB6")


def _pen(color: QColor | str, width: float) -> QPen:
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


class RobotWidget(QWidget):
    PURR_TEXT = "мрр~"  # що «каже» персонаж, коли його гладять
    clicked = Signal()
    petted = Signal()  # почав муркотіти
    files_dropped = Signal(list)  # локальні шляхи
    text_dropped = Signal(str)  # URL або виділений текст
    drag_moved = Signal(QPointF)  # глобальна позиція курсора під час перетягування
    drag_finished = Signal()
    context_requested = Signal(QPointF)
    hold_started = Signal()  # утримує персонажа — почати слухати (голос)
    hold_finished = Signal()  # відпустив — надіслати сказане
    HOLD_MS = 450

    def __init__(self, scale: float = 1.0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.scale_factor = scale
        self.setFixedSize(int(BASE_W * scale), int(BASE_H * scale))

        self.state = "offline"
        self._state_until = 0.0
        self._after_state = "idle"
        self.t = 0.0
        self._last = time.monotonic()
        self._color = QColor(STATE_COLORS["offline"])

        # моргання
        self._next_blink = time.monotonic() + random.uniform(2, 5)
        self._blink = 0.0  # 0 — відкриті, 1 — закриті
        # погляд
        self._gaze = QPointF(0, 0)
        # махання рукою при наведенні
        self._hover = False
        self._wave = 0.0
        # «стискання» при кліку
        self._squish = 0.0  # > 0 — «сплющений», < 0 — «витягнутий» (пружина після кліку)
        self._squish_v = 0.0
        # перетягування
        self._press_pos: QPointF | None = None
        self._dragging = False
        # гладження
        self._pet = 0.0  # «накопичене» гладження
        self._purr = 0.0  # 0..1 — інтенсивність муркотіння
        self._pet_last: tuple[QPointF, float] | None = None
        self._hearts: list[list[float]] = []  # [x, y, вік, дрейф]
        self._next_heart = 0.0
        # голос: утримання персонажа — «говори», рівень звуку (0..1) задає застосунок
        self.hold_enabled = False
        self._holding = False
        self._hold_timer = QTimer(self, singleShot=True, interval=self.HOLD_MS)
        self._hold_timer.timeout.connect(self._on_hold)
        self.voice_level = 0.0
        self._level = 0.0
        # перетягування файлів
        self._drop_hover = False
        self.setAcceptDrops(True)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    # ---------------- керування станом ----------------
    def set_state(self, state: str, duration: float | None = None, then: str = "idle") -> None:
        if state not in STATE_COLORS:
            state = "idle"
        self.state = state
        if duration:
            self._state_until = time.monotonic() + duration
            self._after_state = then
        else:
            self._state_until = 0.0

    def poke(self) -> None:
        """Клік: м'який «желейний» поштовх — сплющується, трохи витягується й заспокоюється (~0.8 с)."""
        self._squish_v = 10.0

    @property
    def purring(self) -> bool:
        return self._purr > 0.5 and self.state in PETTABLE

    # ---------------- анімаційний цикл ----------------
    def _tick(self) -> None:
        now = time.monotonic()
        dt = min(now - self._last, 0.1)
        self._last = now
        self.t += dt

        if self._state_until and now >= self._state_until:
            self._state_until = 0.0
            self.state = self._after_state

        # гладження: накопичене поступово «вивітрюється»
        self._pet = max(0.0, self._pet - dt * 0.7)
        was_purring = self._purr > 0.5
        want = 1.0 if (self._pet > 1.0 and self.state in PETTABLE) else 0.0
        self._purr = _lerp(self._purr, want, min(1.0, dt * (4 if want else 1.5)))
        if self._purr > 0.5 and not was_purring:
            self.petted.emit()
        if self._purr > 0.5 and now >= self._next_heart:
            self._next_heart = now + random.uniform(0.25, 0.5)
            self._hearts.append([random.uniform(60, 140), 40.0, 0.0, random.uniform(-15, 15)])
        for h in self._hearts:
            h[1] -= 28 * dt
            h[0] += h[3] * dt
            h[2] += dt
        self._hearts = [h for h in self._hearts if h[2] < 1.6]

        # рівень голосу: швидко вгору, плавно вниз
        lv = max(0.0, min(1.0, self.voice_level))
        self._level = lv if lv > self._level else _lerp(self._level, lv, min(1.0, dt * 10))

        # плавна зміна кольору
        if self._drop_hover:
            target = STATE_COLORS["happy"]
        else:
            target = PURR_COLOR if self.purring else STATE_COLORS[self.state]
        k = min(1.0, dt * 6)
        self._color = QColor.fromRgbF(
            _lerp(self._color.redF(), target.redF(), k),
            _lerp(self._color.greenF(), target.greenF(), k),
            _lerp(self._color.blueF(), target.blueF(), k),
        )

        # моргання (у сні очі закриті й так)
        if now >= self._next_blink:
            self._blink = 1.0
            self._next_blink = now + random.uniform(2.5, 6.0)
            if random.random() < 0.2:  # іноді подвійне моргання
                self._next_blink = now + 0.35
        self._blink = max(0.0, self._blink - dt * 7)

        # погляд: за курсором у idle/talking/waiting, вгору при роздумах, бігає при роботі
        if self.state in {"idle", "talking", "waiting", "happy", "listening"}:
            center = self.mapToGlobal(self.rect().center())
            d = QCursor.pos() - center
            dist = math.hypot(d.x(), d.y()) or 1.0
            m = min(1.0, dist / 400.0)
            target_gaze = QPointF(d.x() / dist * m, d.y() / dist * m)
        elif self.state == "thinking":
            target_gaze = QPointF(0.6 + 0.2 * math.sin(self.t * 1.3), -0.8)
        elif self.state == "working":
            target_gaze = QPointF(math.sin(self.t * 3.0) * 0.9, 0.35)
        else:
            target_gaze = QPointF(0, 0.2)
        kg = min(1.0, dt * 8)
        self._gaze = QPointF(_lerp(self._gaze.x(), target_gaze.x(), kg), _lerp(self._gaze.y(), target_gaze.y(), kg))

        target_wave = 1.0 if (self._hover and self.state in {"idle", "happy"}) else 0.0
        self._wave = _lerp(self._wave, target_wave, min(1.0, dt * 5))
        # пружина «желе»: м'яка й неквапна, із невеликим відскоком
        steps = max(1, int(dt / 0.01))
        h = dt / steps
        for _ in range(steps):
            self._squish_v += (-80.0 * self._squish - 7.5 * self._squish_v) * h
            self._squish += self._squish_v * h
        self.update()

    # ---------------- малювання ----------------
    def paintEvent(self, _event) -> None:  # noqa: C901
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self.scale_factor, self.scale_factor)

        t, c = self.t, self._color
        # що малювати: перетягування файлу й муркотіння «перекривають» поточний стан
        face_st = body_st = self.state
        if self._drop_hover:
            face_st = body_st = "happy"
        elif self.purring:
            face_st, body_st = "happy", "idle"
        st = body_st
        speed = {"working": 6.0, "thinking": 3.0, "sleeping": 1.0, "happy": 5.0}.get(st, 2.2)
        amp = {"sleeping": 2.0, "happy": 7.0, "offline": 0.0}.get(st, 4.0)
        bob = math.sin(t * speed) * amp
        if st == "happy":
            bob = -abs(math.sin(t * speed)) * amp  # підстрибує
        tilt = {"sad": -8.0, "sleeping": 6.0, "offline": 4.0}.get(st, math.sin(t * 1.1) * 2.0)
        if st == "thinking":
            tilt = 7.0 + math.sin(t * 2) * 2
        if self._purr > 0.05 and not self._drop_hover:
            # блаженне погойдування головою
            tilt = _lerp(tilt, math.sin(t * 5) * 6, self._purr)

        # тінь
        shadow_w = 70 - bob * 1.5
        sg = QRadialGradient(QPointF(100, 205), shadow_w)
        sg.setColorAt(0, QColor(0, 0, 0, 70))
        sg.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(sg)
        p.drawEllipse(QPointF(100, 205), shadow_w, 9)

        p.save()
        squish = self._squish * 0.12
        p.translate(100, 200 + bob)
        p.scale(1 + squish, 1 - squish)
        p.translate(-100, -200)

        self._draw_body(p, c, t, st)

        # голова з нахилом
        p.save()
        p.translate(100, 115)
        p.rotate(tilt)
        p.translate(-100, -115)
        self._draw_head(p, c, t, face_st)
        p.restore()
        p.restore()

        if self._drop_hover:
            self._draw_drop_hint(p, c, t, bob)
        else:
            self._draw_overlay(p, c, t, self.state if not self.purring else "idle", bob)
        self._draw_purr(p, t, bob)
        p.end()

    def _draw_drop_hint(self, p: QPainter, c: QColor, t: float, bob: float) -> None:
        rect = QRectF(140, 0 + bob, 56, 36)
        p.setPen(QPen(QColor("#5B6B82"), 2))
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawRoundedRect(rect, 14, 14)
        f = QFont("Sans", 16, QFont.Weight.Black)
        p.setFont(f)
        p.setPen(c)
        p.drawText(rect.translated(0, 2 * math.sin(t * 8)), Qt.AlignmentFlag.AlignCenter, "⬇")

    def _draw_purr(self, p: QPainter, t: float, bob: float) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        for x, y, age, _ in self._hearts:
            a = max(0.0, 1.0 - age / 1.6)
            col = QColor(PURR_COLOR)
            col.setAlphaF(a)
            p.setBrush(col)
            self._heart(p, QPointF(x, y + bob), 5 + 3 * age)
        if self._purr > 0.6:
            f = QFont("Sans", 10, QFont.Weight.Bold)
            p.setFont(f)
            col = QColor("#E8589A")
            col.setAlphaF(min(1.0, (self._purr - 0.6) * 2.5) * (0.6 + 0.4 * math.sin(t * 4)))
            p.setPen(col)
            p.drawText(QPointF(150, 70 + bob + 3 * math.sin(t * 3)), self.PURR_TEXT)

    def _metal(self, rect: QRectF, light: str = "#FFFFFF", dark: str = "#C9D6E8") -> QLinearGradient:
        g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        g.setColorAt(0, QColor(light))
        g.setColorAt(1, QColor(dark))
        return g

    def _draw_body(self, p: QPainter, c: QColor, t: float, st: str) -> None:
        outline = QPen(QColor("#5B6B82"), 2.5)
        # руки
        for side in (-1, 1):
            p.save()
            p.translate(100 + side * 43, 140)
            angle = side * 12.0
            if st == "working":
                angle = side * (20 + 15 * math.sin(t * 10 + (0 if side < 0 else math.pi)))
            elif st == "happy":
                angle = side * (120 + 20 * math.sin(t * 8))
            elif st == "sad":
                angle = side * 4.0
            elif st == "thinking" and side > 0:
                angle = 140 + 5 * math.sin(t * 2)  # «рука під підборіддям»
            if side < 0 and self._wave > 0.01:
                angle = _lerp(angle, -(130 + 25 * math.sin(t * 10)), self._wave)
            p.rotate(-angle)  # додатний кут — рука відводиться назовні
            arm = QRectF(-7, -4, 14, 36)
            p.setPen(outline)
            p.setBrush(self._metal(arm))
            p.drawRoundedRect(arm, 7, 7)
            p.setBrush(QColor("#E3ECF7"))
            p.drawEllipse(QPointF(0, 34), 8, 8)
            p.restore()

        # тулуб
        body = QRectF(58, 118, 84, 70)
        p.setPen(outline)
        p.setBrush(self._metal(body, "#FDFEFF", "#C3D2E6"))
        p.drawRoundedRect(body, 26, 26)

        # «серце»-індикатор на грудях
        beat = 1.0 + 0.15 * max(0.0, math.sin(t * (6 if st == "working" else 3)))
        glow = QRadialGradient(QPointF(100, 152), 16 * beat)
        gc = QColor(c)
        gc.setAlpha(140)
        glow.setColorAt(0, gc)
        glow.setColorAt(1, QColor(c.red(), c.green(), c.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QPointF(100, 152), 16 * beat, 16 * beat)
        p.setBrush(c)
        self._heart(p, QPointF(100, 152), 7 * beat)

        # ніжки
        p.setPen(outline)
        p.setBrush(QColor("#AFC0D6"))
        for x in (80, 120):
            p.drawRoundedRect(QRectF(x - 9, 184, 18, 12), 6, 6)

    def _heart(self, p: QPainter, center: QPointF, size: float) -> None:
        x, y = center.x(), center.y()
        path = QPainterPath()
        path.moveTo(x, y + size * 0.9)
        path.cubicTo(x - size * 1.6, y - size * 0.1, x - size * 0.7, y - size * 1.3, x, y - size * 0.45)
        path.cubicTo(x + size * 0.7, y - size * 1.3, x + size * 1.6, y - size * 0.1, x, y + size * 0.9)
        p.drawPath(path)

    def _draw_head(self, p: QPainter, c: QColor, t: float, st: str) -> None:
        outline = QPen(QColor("#5B6B82"), 2.5)

        # антена
        p.setPen(_pen("#5B6B82", 3))
        sway = math.sin(t * 2.5) * 4
        p.drawLine(QPointF(100, 40), QPointF(100 + sway, 18))
        pulse = 0.5 + 0.5 * math.sin(t * (8 if st in {"thinking", "working", "waiting"} else 2))
        if st in {"sleeping", "offline"}:
            pulse *= 0.3
        ball = QPointF(100 + sway, 14)
        halo = QRadialGradient(ball, 18)
        hc = QColor(c)
        hc.setAlpha(int(60 + 120 * pulse))
        halo.setColorAt(0, hc)
        halo.setColorAt(1, QColor(c.red(), c.green(), c.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(ball, 18, 18)
        p.setPen(outline)
        p.setBrush(c.lighter(110 + int(30 * pulse)))
        p.drawEllipse(ball, 7, 7)

        # «вушка»
        for x in (30, 170):
            p.setBrush(self._metal(QRectF(x - 9, 65, 18, 36), "#E8F0FA", "#AFC0D6"))
            p.drawRoundedRect(QRectF(x - 9, 64, 18, 38), 8, 8)

        # голова
        head = QRectF(36, 38, 128, 90)
        p.setPen(outline)
        p.setBrush(self._metal(head, "#FFFFFF", "#CCD9EA"))
        p.drawRoundedRect(head, 34, 34)

        # екран-обличчя
        screen = QRectF(50, 52, 100, 62)
        sg = QLinearGradient(screen.topLeft(), screen.bottomLeft())
        sg.setColorAt(0, QColor("#1D2740"))
        sg.setColorAt(1, QColor("#0E1426"))
        p.setPen(QPen(QColor("#3A4A66"), 2))
        p.setBrush(sg)
        p.drawRoundedRect(screen, 22, 22)
        # відблиск
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 22))
        p.drawRoundedRect(QRectF(58, 56, 50, 10), 5, 5)

        self._draw_face(p, c, t, st)

    def _draw_face(self, p: QPainter, c: QColor, t: float, st: str) -> None:
        gx, gy = self._gaze.x() * 7, self._gaze.y() * 5
        eye_y = 80 + gy
        lx, rx = 78 + gx, 122 + gx
        pen = _pen(c, 4.5)

        def glow_ellipse(center: QPointF, w: float, h: float) -> None:
            g = QRadialGradient(center, max(w, h) * 1.8)
            gc = QColor(c)
            gc.setAlpha(90)
            g.setColorAt(0, gc)
            g.setColorAt(1, QColor(c.red(), c.green(), c.blue(), 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(center, w * 1.8, h * 1.8)
            p.setBrush(c)
            p.drawEllipse(center, w, h)
            # блік
            p.setBrush(QColor(255, 255, 255, 200))
            p.drawEllipse(QPointF(center.x() + w * 0.35, center.y() - h * 0.4), w * 0.3, w * 0.3)

        p.setBrush(Qt.BrushStyle.NoBrush)
        if st == "happy":
            p.setPen(pen)
            for x in (lx, rx):
                path = QPainterPath(QPointF(x - 9, eye_y + 3))
                path.quadTo(QPointF(x, eye_y - 10), QPointF(x + 9, eye_y + 3))
                p.drawPath(path)
        elif st in {"sleeping", "offline"}:
            p.setPen(pen)
            for x in (lx, rx):
                if st == "sleeping":
                    path = QPainterPath(QPointF(x - 9, eye_y))
                    path.quadTo(QPointF(x, eye_y + 7), QPointF(x + 9, eye_y))
                    p.drawPath(path)
                else:
                    p.drawLine(QPointF(x - 8, eye_y), QPointF(x + 8, eye_y))
        elif st == "sad":
            p.setPen(pen)
            p.drawLine(QPointF(lx - 9, eye_y - 7), QPointF(lx + 7, eye_y - 3))
            p.drawLine(QPointF(rx + 9, eye_y - 7), QPointF(rx - 7, eye_y - 3))
            glow_ellipse(QPointF(lx, eye_y + 4), 5, 5)
            glow_ellipse(QPointF(rx, eye_y + 4), 5, 5)
        elif st == "working":
            # зосереджені «прищурені» очі
            h = 4.0
            glow_ellipse(QPointF(lx, eye_y), 8, h)
            glow_ellipse(QPointF(rx, eye_y), 8, h)
        else:
            open_k = 1.0 - self._blink
            big = {"waiting": 1.15, "listening": 1.2}.get(st, 1.0)
            h = max(1.5, 9.5 * open_k * big)
            glow_ellipse(QPointF(lx, eye_y), 7.5 * big, h)
            glow_ellipse(QPointF(rx, eye_y), 7.5 * big, h)

        # рум'янець
        if st in {"happy", "talking"} or self._hover:
            p.setPen(Qt.PenStyle.NoPen)
            alpha = 90 if st == "happy" else 55
            p.setBrush(QColor(255, 120, 160, int(alpha + 80 * self._purr)))
            p.drawEllipse(QPointF(62, 96), 7, 4)
            p.drawEllipse(QPointF(138, 96), 7, 4)

        # рот
        mx, my = 100 + gx * 0.4, 100
        p.setBrush(Qt.BrushStyle.NoBrush)
        mouth_pen = _pen(c, 3)
        p.setPen(mouth_pen)
        if st == "talking":
            if self._level > 0.02:  # озвучує відповідь — рот у такт голосу
                open_h = 2 + 9 * self._level
            else:
                open_h = 2 + 5 * abs(math.sin(t * 14)) * (0.6 + 0.4 * math.sin(t * 3.7))
            p.setBrush(c.darker(160))
            p.drawRoundedRect(QRectF(mx - 7, my - open_h / 2, 14, open_h), 4, 4)
        elif st in {"happy", "idle"} or self._hover:
            path = QPainterPath(QPointF(mx - 8, my - 1))
            path.quadTo(QPointF(mx, my + (8 if st == "happy" else 5)), QPointF(mx + 8, my - 1))
            p.drawPath(path)
        elif st == "sad":
            path = QPainterPath(QPointF(mx - 7, my + 3))
            path.quadTo(QPointF(mx, my - 4), QPointF(mx + 7, my + 3))
            p.drawPath(path)
        elif st in {"waiting", "listening"}:
            p.drawEllipse(QPointF(mx, my + 1), 3.5, 3.5)
        elif st == "sleeping":
            p.drawEllipse(QPointF(mx, my + 1), 2.5 + math.sin(t) * 0.8, 2.5 + math.sin(t) * 0.8)
        else:
            p.drawLine(QPointF(mx - 6, my), QPointF(mx + 6, my))

    def _draw_overlay(self, p: QPainter, c: QColor, t: float, st: str, bob: float) -> None:
        if st == "thinking":
            # три точки, що обертаються навколо голови
            for i in range(3):
                a = t * 2.4 + i * (2 * math.pi / 3)
                pos = QPointF(100 + math.cos(a) * 82, 82 + bob + math.sin(a) * 18)
                size = 4 + 1.5 * math.sin(a)
                cc = QColor(c)
                cc.setAlpha(150 + int(100 * (math.sin(a) + 1) / 2))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(cc)
                p.drawEllipse(pos, size, size)
        elif st == "working":
            self._gear(p, QPointF(166, 30 + bob), 13, t * 3, c)
            self._gear(p, QPointF(184, 48 + bob), 8, -t * 4.5, c.darker(115))
        elif st == "sleeping":
            p.setPen(QColor("#7C93D9"))
            for i in range(3):
                phase = (t * 0.5 + i / 3) % 1.0
                f = QFont("Sans", int(9 + 7 * phase), QFont.Weight.Bold)
                p.setFont(f)
                cc = QColor("#7C93D9")
                cc.setAlphaF(1 - phase)
                p.setPen(cc)
                p.drawText(QPointF(140 + phase * 30 + i * 2, 40 - phase * 34 + bob), "z")
        elif st == "waiting":
            k = 0.8 + 0.2 * math.sin(t * 6)
            rect = QRectF(150, 0 + bob, 40, 36)
            p.setPen(QPen(QColor("#5B6B82"), 2))
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawRoundedRect(rect, 14, 14)
            f = QFont("Sans", 18, QFont.Weight.Black)
            p.setFont(f)
            cc = QColor(c)
            cc.setAlphaF(k)
            p.setPen(cc)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "?")
        elif st == "listening":
            self._draw_sound_waves(p, QPointF(100, 83 + bob), c, t, spread=78)  # від «вушок»
        elif st == "offline":
            f = QFont("Sans", 14, QFont.Weight.Bold)
            p.setFont(f)
            p.setPen(QColor("#8A94A6"))
            p.drawText(QRectF(140, 0, 60, 30), Qt.AlignmentFlag.AlignCenter, "…")
        elif st == "happy":
            # іскорки
            for i in range(4):
                a = i * math.pi / 2 + t
                r = 88 + 6 * math.sin(t * 5 + i)
                pos = QPointF(100 + math.cos(a) * r, 90 + bob + math.sin(a) * 50)
                self._sparkle(p, pos, 4 + 2 * math.sin(t * 6 + i), c)

    def _draw_sound_waves(self, p: QPainter, center: QPointF, c: QColor, t: float, spread: float = 0) -> None:
        """«Слухаю»: звукові дуги в обидва боки від center (± spread) — що голосніше говориш, то ширші й яскравіші."""
        lv = self._level
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(3):
            phase = (t * 1.4 + i / 3) % 1.0
            r = 6 + 16 * phase + 5 * lv
            cc = QColor(c)
            cc.setAlphaF(max(0.0, (1 - phase) * (0.45 + 0.55 * min(1.0, lv * 2 + 0.1))))
            p.setPen(_pen(cc, 2.6 + 1.6 * lv))
            for side, start in ((1, -40), (-1, 140)):
                o = QPointF(center.x() + side * spread, center.y())
                p.drawArc(QRectF(o.x() - r, o.y() - r, 2 * r, 2 * r), start * 16, 80 * 16)

    def _gear(self, p: QPainter, center: QPointF, r: float, angle: float, color: QColor) -> None:
        p.save()
        p.translate(center)
        p.rotate(math.degrees(angle))
        path = QPainterPath()
        teeth = 8
        for i in range(teeth * 2):
            a = i * math.pi / teeth
            rr = r if i % 2 == 0 else r * 0.75
            pt = QPointF(math.cos(a) * rr, math.sin(a) * rr)
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        path.closeSubpath()
        p.setPen(QPen(QColor("#5B6B82"), 1.5))
        p.setBrush(color)
        p.drawPath(path)
        p.setBrush(QColor("#1D2740"))
        p.drawEllipse(QPointF(0, 0), r * 0.3, r * 0.3)
        p.restore()

    def _sparkle(self, p: QPainter, c: QPointF, s: float, color: QColor) -> None:
        path = QPainterPath(QPointF(c.x(), c.y() - s * 2))
        path.quadTo(c, QPointF(c.x() + s * 2, c.y()))
        path.quadTo(c, QPointF(c.x(), c.y() + s * 2))
        path.quadTo(c, QPointF(c.x() - s * 2, c.y()))
        path.quadTo(c, QPointF(c.x(), c.y() - s * 2))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color.lighter(130)))
        p.drawPath(path)

    # ---------------- миша ----------------
    def enterEvent(self, _e) -> None:
        self._hover = True

    def leaveEvent(self, _e) -> None:
        self._hover = False
        self._pet_last = None

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.globalPosition()
            self._dragging = False
            self._holding = False
            if self.hold_enabled:
                self._hold_timer.start()
        elif e.button() == Qt.MouseButton.RightButton:
            self.context_requested.emit(e.globalPosition())

    def _track_pet(self, pos: QPointF) -> None:
        """Повільні рухи курсора по голові накопичують «гладження»."""
        local = QPointF(pos.x() / self.scale_factor, pos.y() / self.scale_factor)
        now = time.monotonic()
        if HEAD_RECT.contains(local) and self._pet_last is not None:
            prev, prev_t = self._pet_last
            dist = math.hypot(local.x() - prev.x(), local.y() - prev.y())
            speed = dist / max(now - prev_t, 1e-3)  # базових одиниць за секунду
            if 20 < speed < 900:  # повільно-помірно — гладимо; різко — ні
                self._pet = min(3.0, self._pet + dist * 0.012)
        self._pet_last = (local, now)

    def mouseMoveEvent(self, e) -> None:
        if self._press_pos is None:
            self._track_pet(e.position())
            return
        if self._holding:
            return  # уже слухає — персонаж стоїть на місці
        if not self._dragging and (e.globalPosition() - self._press_pos).manhattanLength() > 6:
            self._dragging = True
            self._hold_timer.stop()
        if self._dragging:
            self.drag_moved.emit(e.globalPosition() - self._press_pos)
            self._press_pos = e.globalPosition()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton or self._press_pos is None:
            return
        self._hold_timer.stop()
        if self._holding:
            self._holding = False
            self.hold_finished.emit()
        elif self._dragging:
            self.drag_finished.emit()
        else:
            self.poke()
            self.clicked.emit()  # одразу, без очікування подвійного кліку
        self._press_pos = None
        self._dragging = False

    def _on_hold(self) -> None:
        if self._press_pos is not None and not self._dragging:
            self._holding = True
            self.poke()  # «стрепенувся» — почав слухати
            self.hold_started.emit()

    def mouseDoubleClickEvent(self, e) -> None:
        # другий швидкий клік — просто ще один клік (Qt замість натискання надсилає подвійний)
        self.mousePressEvent(e)

    # ---------------- перетягування файлів / тексту ----------------
    def dragEnterEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasUrls() or md.hasText():
            self._drop_hover = True
            e.acceptProposedAction()

    def dragLeaveEvent(self, _e) -> None:
        self._drop_hover = False

    def dropEvent(self, e) -> None:
        self._drop_hover = False
        md = e.mimeData()
        files = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        if files:
            self.files_dropped.emit(files)
        elif md.hasUrls():
            self.text_dropped.emit(" ".join(u.toString() for u in md.urls()))
        elif md.hasText():
            self.text_dropped.emit(md.text())
        self.poke()
        e.acceptProposedAction()
