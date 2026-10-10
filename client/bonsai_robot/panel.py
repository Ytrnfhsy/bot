"""Спливаюча панель: поле вводу задачі, хід виконання, відповідь, підтвердження дій."""

from __future__ import annotations

import html

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

STYLE = """
#card { background: rgba(250, 252, 255, 245); border: 1px solid #C9D6E8; border-radius: 18px; }
QLabel#status { color: #5B6B82; font-size: 11px; }
QLabel#activity { color: #7A5AF8; font-size: 11px; }
QTextBrowser { background: transparent; border: none; color: #1D2740; font-size: 13px; }
QPlainTextEdit { background: #FFFFFF; border: 1px solid #C9D6E8; border-radius: 12px;
                 padding: 6px 8px; color: #1D2740; font-size: 13px; }
QPlainTextEdit:focus { border: 1px solid #4FD1FF; }
QPushButton { border: none; border-radius: 12px; padding: 6px 12px; font-weight: 600; }
QPushButton#send { background: #4FD1FF; color: #0E1426; }
QPushButton#send:hover { background: #7BDDFF; }
QPushButton#stop { background: #FFE1E1; color: #C92A2A; }
QPushButton#flat { background: transparent; color: #5B6B82; padding: 0; border-radius: 8px; }
QPushButton#flat:hover { background: #E7EEF8; }
QPushButton#flat:pressed { background: #D5E2F2; }
QPushButton#allow { background: #5BE49B; color: #0E3B22; }
QPushButton#deny { background: #FFE1E1; color: #C92A2A; }
#confirm { background: #FFF6E9; border: 1px solid #FFC98A; border-radius: 12px; }
QLabel#confirmTitle { color: #B35C00; font-weight: 700; }
QLabel#confirmText { color: #3A2A10; font-family: monospace; font-size: 11px; }
"""


CARD_FILL = QColor(250, 252, 255, 245)
CARD_BORDER = QColor("#C9D6E8")
ICON_COLOR = QColor("#5B6B82")


def make_icon(kind: str, size: int = 18) -> QIcon:
    """Прості векторні іконки (не залежать від емодзі-шрифтів і теми системи)."""
    scale = 3  # малюємо з запасом — чітко і на HiDPI
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(scale * size / 24, scale * size / 24)  # координати 24×24
    pen = QPen(ICON_COLOR, 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "save":  # дискета
        body = QPainterPath()
        body.moveTo(5, 4)
        body.lineTo(16, 4)
        body.lineTo(20, 8)
        body.lineTo(20, 20)
        body.lineTo(4, 20)
        body.lineTo(4, 5)
        body.closeSubpath()
        p.drawPath(body)
        p.drawRect(QRectF(8, 4, 7, 5))
        p.drawRoundedRect(QRectF(7, 13, 10, 7), 1, 1)
    elif kind == "chats":  # тека
        path = QPainterPath()
        path.moveTo(3, 7)
        path.lineTo(3, 18.5)
        path.quadTo(3, 20, 4.5, 20)
        path.lineTo(19.5, 20)
        path.quadTo(21, 20, 21, 18.5)
        path.lineTo(21, 9.5)
        path.quadTo(21, 8, 19.5, 8)
        path.lineTo(12, 8)
        path.lineTo(10, 5)
        path.lineTo(4.5, 5)
        path.quadTo(3, 5, 3, 6.5)
        p.drawPath(path)
        p.drawLine(QPointF(3, 11), QPointF(21, 11))
    elif kind == "reset":  # кругова стрілка
        p.drawArc(QRectF(5, 5, 14, 14), 60 * 16, 290 * 16)
        p.drawPolyline(QPolygonF([QPointF(15.5, 3.5), QPointF(16.5, 6.8), QPointF(13.2, 7.8)]))
    elif kind == "close":
        p.drawLine(QPointF(7, 7), QPointF(17, 17))
        p.drawLine(QPointF(17, 7), QPointF(7, 17))
    p.end()
    return QIcon(pm)


class InputBox(QPlainTextEdit):
    submitted = Signal()
    image_pasted = Signal(str)  # шлях до тимчасового PNG

    def canInsertFromMimeData(self, source) -> bool:
        return source.hasImage() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:
        """Ctrl+V зі скриншотом (або перетягнута в поле картинка) → тимчасовий PNG для завантаження."""
        if source.hasImage() and not source.hasText():
            import tempfile
            import time

            image = source.imageData()
            if image is not None and not image.isNull():
                path = f"{tempfile.gettempdir()}/скриншот-{time.strftime('%H%M%S')}.png"
                if image.save(path, "PNG"):
                    self.image_pasted.emit(path)
                    return
        super().insertFromMimeData(source)

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(e)


class ChatPanel(QWidget):
    task_submitted = Signal(str)
    image_pasted = Signal(str)
    cancel_requested = Signal()
    reset_requested = Signal()
    save_requested = Signal()
    chats_requested = Signal()
    confirm_answered = Signal(str, bool)
    closed = Signal()

    WIDTH = 408
    MARGIN = 14  # поле навколо картки (тінь + «хвостик»)

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        self.setFixedWidth(self.WIDTH)
        # «хвостик» бульбашки в бік робота та анімація появи
        self._tail_side: str | None = None  # "left" | "right"
        self._tail_y = 0
        self._anim: QParallelAnimationGroup | None = None
        self._pos_anim: QPropertyAnimation | None = None
        self._hiding = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(self.MARGIN, 10, self.MARGIN, 10)  # збоку — місце для «хвостика» до робота
        card = QFrame(objectName="card")
        shadow = QGraphicsDropShadowEffect(blurRadius=24, xOffset=0, yOffset=4)
        shadow.setColor(Qt.GlobalColor.gray)
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(8)

        # заголовок
        head = QHBoxLayout()
        self.status = QLabel("● не підключено", objectName="status")
        self.status.setWordWrap(True)
        head.addWidget(self.status, 1)
        self.btn_save = QPushButton(objectName="flat", toolTip="Зберегти чат (Ctrl+S)")
        self.btn_save.clicked.connect(self.save_requested)
        self.btn_chats = QPushButton(objectName="flat", toolTip="Збережені чати")
        self.btn_chats.clicked.connect(self.chats_requested)
        self.btn_reset = QPushButton(objectName="flat", toolTip="Нова розмова")
        self.btn_reset.clicked.connect(self.reset_requested)
        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save_requested.emit)
        self.btn_close = QPushButton(objectName="flat", toolTip="Сховати (Esc)")
        self.btn_close.clicked.connect(self.hide_panel)
        for btn, kind in ((self.btn_save, "save"), (self.btn_chats, "chats"),
                          (self.btn_reset, "reset"), (self.btn_close, "close")):
            btn.setIcon(make_icon(kind))
            btn.setIconSize(QSize(18, 18))
            btn.setFixedSize(28, 28)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.hide_panel)
        head.addWidget(self.btn_save)
        head.addWidget(self.btn_chats)
        head.addWidget(self.btn_reset)
        head.addWidget(self.btn_close)
        lay.addLayout(head)

        # стрічка розмови
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setMinimumHeight(0)
        self.view.setMaximumHeight(420)
        self.view.hide()
        lay.addWidget(self.view)

        self.activity = QLabel("", objectName="activity")
        self.activity.setWordWrap(True)
        self.activity.hide()
        lay.addWidget(self.activity)

        # підтвердження
        self.confirm_box = QFrame(objectName="confirm")
        cl = QVBoxLayout(self.confirm_box)
        cl.setContentsMargins(10, 8, 10, 8)
        cl.addWidget(QLabel("⚠ Агент просить дозволу", objectName="confirmTitle"))
        self.confirm_text = QLabel("", objectName="confirmText")
        self.confirm_text.setWordWrap(True)
        self.confirm_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.confirm_text)
        bl = QHBoxLayout()
        self.btn_allow = QPushButton("Дозволити", objectName="allow")
        self.btn_deny = QPushButton("Відхилити", objectName="deny")
        self.btn_allow.clicked.connect(lambda: self._answer(True))
        self.btn_deny.clicked.connect(lambda: self._answer(False))
        bl.addStretch(1)
        bl.addWidget(self.btn_deny)
        bl.addWidget(self.btn_allow)
        cl.addLayout(bl)
        self.confirm_box.hide()
        lay.addWidget(self.confirm_box)
        self._confirm_queue: list[tuple[str, str]] = []

        # ввід
        row = QHBoxLayout()
        self.input = InputBox()
        self.input.setPlaceholderText("Що зробити? (Enter — надіслати, Shift+Enter — новий рядок, Ctrl+V — вставити скриншот)")
        self.input.setFixedHeight(64)
        self.input.submitted.connect(self._submit)
        self.input.image_pasted.connect(self.image_pasted)
        row.addWidget(self.input, 1)
        btns = QVBoxLayout()
        self.btn_send = QPushButton("➤", objectName="send", toolTip="Надіслати")
        self.btn_send.clicked.connect(self._submit)
        self.btn_stop = QPushButton("■", objectName="stop", toolTip="Зупинити")
        self.btn_stop.clicked.connect(self.cancel_requested)
        self.btn_stop.hide()
        btns.addWidget(self.btn_send)
        btns.addWidget(self.btn_stop)
        row.addLayout(btns)
        lay.addLayout(row)

        # транскрипт: список (роль, текст)
        self.entries: list[tuple[str, str]] = []
        self.draft = ""
        self._render_timer = QTimer(self, singleShot=True, interval=60)
        self._render_timer.timeout.connect(self._render)

    # ---------------- API ----------------
    # ---------------- поява / зникнення ----------------
    SHOW_MS, HIDE_MS, SLIDE = 260, 170, 22

    def set_tail(self, side: str | None, y: int) -> None:
        """З якого боку робот (туди дивиться «хвостик») і на якій висоті (у координатах панелі)."""
        if (side, y) != (self._tail_side, self._tail_y):
            self._tail_side, self._tail_y = side, y
            self.update()

    def _offset_toward_robot(self) -> QPoint:
        dx = {"right": self.SLIDE, "left": -self.SLIDE}.get(self._tail_side or "", 0)
        return QPoint(dx, 10)

    def _animate(self, start: QPoint, end: QPoint, opacity: tuple[float, float], ms: int,
                 curve: QEasingCurve.Type, on_done=None) -> None:
        if self._anim is not None:
            self._anim.stop()
        group = QParallelAnimationGroup(self)
        pos = QPropertyAnimation(self, b"pos", group)
        pos.setStartValue(start)
        pos.setEndValue(end)
        pos.setDuration(ms)
        pos.setEasingCurve(curve)
        fade = QPropertyAnimation(self, b"windowOpacity", group)
        fade.setStartValue(opacity[0])
        fade.setEndValue(opacity[1])
        fade.setDuration(ms)
        fade.setEasingCurve(QEasingCurve.Type.OutQuad if opacity[1] > opacity[0] else QEasingCurve.Type.InQuad)
        group.addAnimation(pos)
        group.addAnimation(fade)
        if on_done:
            group.finished.connect(on_done)
        self._anim, self._pos_anim = group, pos
        group.start()

    def move_to(self, target: QPoint) -> None:
        """Перемістити панель; якщо вона саме з'являється — змінити кінцеву точку анімації."""
        if self._anim is not None and self._anim.state() == QParallelAnimationGroup.State.Running and not self._hiding:
            self._pos_anim.setEndValue(target)
        else:
            self.move(target)

    def show_panel(self, target: QPoint | None = None) -> None:
        if target is None:
            target = self.pos()
        if self.isVisible() and not self._hiding:
            self.move_to(target)
        else:
            # виїжджає від робота з легким «проявом»; якщо саме ховалась — продовжує з поточного місця
            start = self.pos() if self._hiding else target + self._offset_toward_robot()
            opacity = self.windowOpacity() if self._hiding else 0.0
            self._hiding = False
            self.setWindowOpacity(opacity)
            self.move(start)
            self.show()
            self._animate(start, target, (opacity, 1.0), self.SHOW_MS, QEasingCurve.Type.OutBack)
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def hide_panel(self) -> None:
        if not self.isVisible() or self._hiding:
            return
        self._hiding = True

        def done() -> None:
            if self._hiding:
                self._hiding = False
                self.hide()
                self.setWindowOpacity(1.0)

        self._animate(self.pos(), self.pos() + self._offset_toward_robot(), (self.windowOpacity(), 0.0),
                      self.HIDE_MS, QEasingCurve.Type.InCubic, done)
        self.closed.emit()

    @property
    def shown(self) -> bool:
        """Видима і не в процесі зникнення."""
        return self.isVisible() and not self._hiding

    def paintEvent(self, _event) -> None:
        if not self._tail_side:
            return
        # «хвостик» бульбашки: малюємо під карткою, тож видно лише кінчик, що виступає до робота
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        y = max(36, min(self._tail_y, self.height() - 36))
        edge = self.width() - self.MARGIN if self._tail_side == "right" else self.MARGIN
        tip = self.width() - 2 if self._tail_side == "right" else 2
        inner = edge - 14 if self._tail_side == "right" else edge + 14
        poly = QPolygonF([QPointF(inner, y - 11), QPointF(tip, y + 2), QPointF(inner, y + 11)])
        p.setPen(QPen(CARD_BORDER, 1))
        p.setBrush(CARD_FILL)
        p.drawPolygon(poly)
        p.end()

    def set_status(self, text: str, ok: bool, details: str = "") -> None:
        color = "#2F9E44" if ok else "#C92A2A"
        self.status.setText(f'<span style="color:{color}">●</span> {html.escape(text)}')
        self.status.setToolTip(details or text)
        self.adjustSize()

    def prefill(self, text: str) -> None:
        """Підставляє текст у поле вводу (курсор у кінці), щоб користувач дописав і натиснув Enter."""
        self.input.setPlainText(text)
        cur = self.input.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self.input.setTextCursor(cur)
        self.input.setFocus()

    def set_busy(self, busy: bool) -> None:
        self.btn_stop.setVisible(busy)
        self.btn_send.setVisible(not busy)
        if not busy:
            self.set_activity("")

    def set_activity(self, text: str) -> None:
        self.activity.setText(text)
        self.activity.setVisible(bool(text))
        self.adjustSize()

    def add_entry(self, role: str, text: str) -> None:
        self.entries.append((role, text))
        self.entries = self.entries[-60:]
        self.draft = ""
        self._schedule()

    def append_draft(self, text: str) -> None:
        self.draft += text
        self._schedule()

    def clear_draft(self) -> None:
        if self.draft:
            self.draft = ""
            self._schedule()

    def clear(self) -> None:
        self.entries.clear()
        self.draft = ""
        self._render()

    def ask_confirm(self, cid: str, summary: str) -> None:
        self._confirm_queue.append((cid, summary))
        if len(self._confirm_queue) == 1:
            self._show_confirm()

    def drop_confirms(self) -> None:
        self._confirm_queue.clear()
        self.confirm_box.hide()
        self.adjustSize()

    # ---------------- внутрішнє ----------------
    def _show_confirm(self) -> None:
        if not self._confirm_queue:
            self.confirm_box.hide()
        else:
            summary = self._confirm_queue[0][1]
            if len(summary) > 1200:
                summary = summary[:1200] + "…"
            self.confirm_text.setText(summary)
            self.confirm_box.show()
            self.btn_allow.setFocus()
        self.adjustSize()

    def _answer(self, approved: bool) -> None:
        if not self._confirm_queue:
            return
        cid, _ = self._confirm_queue.pop(0)
        self.confirm_answered.emit(cid, approved)
        self._show_confirm()

    def _submit(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        if self.btn_stop.isVisible():
            return  # агент ще працює
        self.input.clear()
        self.task_submitted.emit(text)

    def _schedule(self) -> None:
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _render(self) -> None:
        parts = []
        for role, text in self.entries:
            if role == "user":
                parts.append(f"**🧑 {text}**")
            elif role == "tool":
                parts.append(f"<small>{html.escape(text)}</small>")
            elif role == "error":
                parts.append(f"⚠️ *{text}*")
            else:
                parts.append(text)
        if self.draft:
            parts.append(self.draft + " ▍")
        self.view.setMarkdown("\n\n".join(parts))
        visible = bool(parts)
        self.view.setVisible(visible)
        if visible:
            doc = self.view.document()
            # ширина тексту = ширина картки мінус відступи й вертикальна прокрутка — щоб не було горизонтальної
            doc.setTextWidth(self.WIDTH - 2 * self.MARGIN - 28 - 18)
            doc_h = int(doc.size().height()) + 12
            self.view.setFixedHeight(max(40, min(420, doc_h)))
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())
        self.adjustSize()
