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
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QFrame,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
QPlainTextEdit#confirmText { color: #3A2A10; background: #FFFBF4; border: 1px solid #FFE0B8;
    border-radius: 6px; font-family: monospace; font-size: 11px; }
#ask { background: #EEF6FF; border: 1px solid #B9DBF5; border-radius: 12px; }
QLabel#askText { color: #1D3A57; font-weight: 600; }
QLineEdit#askInput { background: #FFFFFF; border: 1px solid #C9D6E8; border-radius: 8px; padding: 5px 8px; color: #1D2740; }
QLineEdit#askInput:focus { border: 1px solid #4FD1FF; }
QPushButton#primary { background: #4FD1FF; color: #0E1426; }
QPushButton#primary:hover { background: #7BDDFF; }
QPushButton#secondary { background: #E7EEF8; color: #1D2740; }
QPushButton#secondary:hover { background: #D5E2F2; }
QPushButton#danger { background: #FFE1E1; color: #C92A2A; }
QLabel#historyTitle { color: #1D2740; font-weight: 700; font-size: 14px; }
QLabel#historyEmpty { color: #5B6B82; padding: 18px 6px; }
QListWidget#history { background: transparent; border: none; outline: none; font-size: 13px; color: #1D2740; }
QListWidget#history::item { border-radius: 10px; padding: 7px 8px; margin: 1px 0; }
QListWidget#history::item:hover { background: #EEF3FA; }
QListWidget#history::item:selected { background: #DCEFFD; color: #0E1426; }
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
    if kind == "new":  # аркуш з олівцем — новий чат
        p.drawPolyline(QPolygonF([QPointF(12, 4), QPointF(5.5, 4), QPointF(4, 5.5), QPointF(4, 18.5),
                                  QPointF(5.5, 20), QPointF(18.5, 20), QPointF(20, 18.5), QPointF(20, 12)]))
        pencil = QPainterPath()
        pencil.moveTo(18, 3.5)
        pencil.lineTo(20.5, 6)
        pencil.lineTo(12.5, 14)
        pencil.lineTo(9.5, 14.5)
        pencil.lineTo(10, 11.5)
        pencil.closeSubpath()
        p.drawPath(pencil)
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
    height_changed = Signal()  # вміст змінив висоту — панель треба перепозиціонувати біля персонажа
    image_pasted = Signal(str)
    cancel_requested = Signal()
    history_open = Signal(str)
    history_export = Signal(str)
    history_delete = Signal(str)
    history_rename = Signal(str, str)  # (id, поточна назва)
    new_chat_requested = Signal()
    chats_requested = Signal()
    confirm_answered = Signal(str, bool)
    closed = Signal()

    WIDTH = 408
    MARGIN = 14  # поле навколо картки (тінь + «хвостик»)

    def __init__(self) -> None:
        self._refit_pending = False
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
        # тінь малюємо самі в paintEvent: QGraphicsDropShadowEffect інколи не перемальовував дочірні віджети
        self.card = card
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(8)

        # заголовок
        head = QHBoxLayout()
        self.status = QLabel("● не підключено", objectName="status")
        self.status.setWordWrap(True)
        head.addWidget(self.status, 1)
        self.btn_new = QPushButton(objectName="flat", toolTip="Новий чат (Ctrl+N) — поточний уже збережено")
        self.btn_new.clicked.connect(self.new_chat_requested)
        self.btn_chats = QPushButton(objectName="flat", toolTip="Усі чати")
        self.btn_chats.clicked.connect(self.chats_requested)
        QShortcut(QKeySequence.StandardKey.New, self, activated=self.new_chat_requested.emit)
        self.btn_close = QPushButton(objectName="flat", toolTip="Сховати (Esc)")
        self.btn_close.clicked.connect(self.hide_panel)
        for btn, kind in ((self.btn_new, "new"), (self.btn_chats, "chats"), (self.btn_close, "close")):
            btn.setIcon(make_icon(kind))
            btn.setIconSize(QSize(18, 18))
            btn.setFixedSize(28, 28)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._on_escape)
        head.addWidget(self.btn_new)
        head.addWidget(self.btn_chats)
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
        # великий скрипт — у полі з прокруткою: кнопки «Дозволити / Відхилити» завжди лишаються на екрані
        self.confirm_text = QPlainTextEdit(objectName="confirmText")
        self.confirm_text.setReadOnly(True)
        self.confirm_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.confirm_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
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

        # вбудований запит (зберегти? / назва чату / видалити?) — замість окремих вікон
        self.ask_box = QFrame(objectName="ask")
        al = QVBoxLayout(self.ask_box)
        al.setContentsMargins(10, 8, 10, 8)
        al.setSpacing(6)
        self.ask_text = QLabel("", objectName="askText")
        self.ask_text.setWordWrap(True)
        al.addWidget(self.ask_text)
        self.ask_input = QLineEdit(objectName="askInput")
        self.ask_input.returnPressed.connect(lambda: self._ask_answer(self._ask_default))
        al.addWidget(self.ask_input)
        self.ask_buttons = QHBoxLayout()
        al.addLayout(self.ask_buttons)
        self.ask_box.hide()
        lay.addWidget(self.ask_box)
        self._ask_callback = None
        self._ask_default = ""

        # сторінка «Чати» (усі розмови зберігаються автоматично)
        self.history_page = QFrame()
        hl = QVBoxLayout(self.history_page)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(6)
        hl.addWidget(QLabel("Чати", objectName="historyTitle"))
        self.history_empty = QLabel("Чатів поки немає.\nКожна розмова з'явиться тут автоматично.",
                                    objectName="historyEmpty")
        self.history_empty.setWordWrap(True)
        hl.addWidget(self.history_empty)
        self.history_list = QListWidget(objectName="history")
        self.history_list.setMaximumHeight(380)
        self.history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history_list.itemDoubleClicked.connect(lambda _: self._history_action("open"))
        self.history_list.currentItemChanged.connect(lambda *_: self._history_buttons())
        hl.addWidget(self.history_list)
        hb = QHBoxLayout()
        self.btn_h_back = QPushButton("←", objectName="secondary", toolTip="Назад до розмови (Esc)")
        self.btn_h_open = QPushButton("Відкрити", objectName="primary")
        self.btn_h_rename = QPushButton("Назва…", objectName="secondary", toolTip="Перейменувати чат")
        self.btn_h_export = QPushButton(".md", objectName="secondary", toolTip="Експорт у Markdown")
        self.btn_h_delete = QPushButton("Видалити", objectName="danger")
        self.btn_h_back.clicked.connect(self.show_chat)
        self.btn_h_open.clicked.connect(lambda: self._history_action("open"))
        self.btn_h_rename.clicked.connect(lambda: self._history_action("rename"))
        self.btn_h_export.clicked.connect(lambda: self._history_action("export"))
        self.btn_h_delete.clicked.connect(lambda: self._history_action("delete"))
        hb.addWidget(self.btn_h_back)
        hb.addStretch(1)
        for b in (self.btn_h_delete, self.btn_h_rename, self.btn_h_export, self.btn_h_open):
            hb.addWidget(b)
        hl.addLayout(hb)
        self.history_page.hide()
        lay.addWidget(self.history_page)

        # ввід
        self.input_row = QWidget()
        row = QHBoxLayout(self.input_row)
        row.setContentsMargins(0, 0, 0, 0)
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
        lay.addWidget(self.input_row)

        # транскрипт: список (роль, текст)
        self.entries: list[tuple[str, str]] = []
        self.draft = ""
        self._render_timer = QTimer(self, singleShot=True, interval=60)
        self._render_timer.timeout.connect(self._render)

    # ---------------- API ----------------
    # ---------------- поява / зникнення ----------------
    SHOW_MS, HIDE_MS, SLIDE = 190, 150, 18

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
            # поки панель була схована, Qt не враховував у розмітці стрічку, що оновилась за цей час, —
            # перерахувати висоту одразу після показу, інакше поле вводу налазить на текст
            self._fit()
            self._animate(start, target, (opacity, 1.0), self.SHOW_MS, QEasingCurve.Type.OutBack)
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def _on_escape(self) -> None:
        if self.cancel_ask():
            return
        if self.history_shown:
            self.show_chat()
            return
        self.hide_panel()

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
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # м'яка тінь під карткою: кілька напівпрозорих шарів, що розширюються
        r = QRectF(self.card.geometry())
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(1, 9):
            p.setBrush(QColor(30, 45, 70, max(2, 16 - i * 2)))
            p.drawRoundedRect(r.adjusted(-i, -i + 3, i, i + 3), 18 + i, 18 + i)
        if not self._tail_side:
            p.end()
            return
        # «хвостик» бульбашки: малюємо під карткою, тож видно лише кінчик, що виступає до робота
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
        self._fit()

    def _fit(self) -> None:
        """Підігнати висоту під вміст. (adjustSize() у Qt обрізає вікно до ⅔ екрана — тоді запити «зникали».)"""
        lay = self.layout()
        # спершу скинути закешовані розміри: щойно показана/схована стрічка чи рядок активності інакше
        # не враховуються, панель лишається нижчою за вміст, і поле вводу налазить на текст
        lay.invalidate()
        lay.activate()
        # фіксована висота (min = max): віконний менеджер (KWin/XWayland) не може лишити старий, більший
        # розмір, коли вміст зменшився, — інакше після «Новий чат» панель лишалась високою з порожнечею
        h = max(lay.sizeHint().height(), lay.minimumSize().height())
        if h != self.height() or self.minimumHeight() != self.maximumHeight():
            self.setFixedHeight(h)
            self.height_changed.emit()
        if not self._refit_pending:  # і ще раз, коли Qt обробить відкладені зміни геометрії
            self._refit_pending = True
            QTimer.singleShot(0, self._refit)

    def _refit(self) -> None:
        self._refit_pending = False
        lay = self.layout()
        lay.invalidate()
        lay.activate()
        h = max(lay.sizeHint().height(), lay.minimumSize().height())
        if h != self.height():
            self.setFixedHeight(h)
            self.height_changed.emit()

    # ---------------- вбудовані запити ----------------
    def ask(self, text: str, buttons: list[tuple[str, str, str]], callback, input_text: str | None = None,
            default: str = "") -> None:
        """Запит усередині панелі. buttons: [(ключ, підпис, стиль primary|secondary|danger)].

        callback(ключ, введений_текст). Enter у полі = default, Esc = "cancel".
        """
        while self.ask_buttons.count():
            w = self.ask_buttons.takeAt(0).widget()
            if w:
                w.hide()  # прибрати одразу — deleteLater спрацює лише в наступному циклі подій
                w.setParent(None)
                w.deleteLater()
        self.ask_buttons.addStretch(1)
        for key, label, style in buttons:
            b = QPushButton(label, objectName=style)
            b.clicked.connect(lambda _=False, k=key: self._ask_answer(k))
            self.ask_buttons.addWidget(b)
        self.ask_text.setText(text)
        self.ask_input.setVisible(input_text is not None)
        if input_text is not None:
            self.ask_input.setText(input_text)
            self.ask_input.selectAll()
        self._ask_callback, self._ask_default = callback, default
        self.ask_box.show()
        self.ask_box.raise_()
        self._render()
        (self.ask_input if input_text is not None else self.ask_box).setFocus()
        # після перебудови кнопок примусово перемальовуємо (інакше смужка інколи лишалася порожньою)
        self.ask_box.update()
        QTimer.singleShot(0, self.repaint)
        self._fit()

    def _ask_answer(self, key: str) -> None:
        callback, self._ask_callback = self._ask_callback, None
        self.ask_box.hide()
        self._render()
        self._fit()
        if self.history_page.isHidden():
            self.input.setFocus()
        if callback:
            callback(key, self.ask_input.text().strip())

    def cancel_ask(self) -> bool:
        if self.ask_box.isVisible():
            self._ask_answer("cancel")
            return True
        return False

    # ---------------- сторінка історії ----------------
    def show_history(self) -> None:
        for w in (self.view, self.activity, self.input_row):
            w.hide()
        self.history_page.show()
        self.history_list.setFocus()
        self._fit()

    def show_chat(self) -> None:
        self.history_page.hide()
        self.input_row.show()
        self._render()
        self.set_activity(self.activity.text())
        self.input.setFocus()
        self._fit()

    @property
    def history_shown(self) -> bool:
        return not self.history_page.isHidden()

    def set_history(self, chats: list[dict], active: str | None = None) -> None:
        """active — id чату, відкритого зараз (позначається ●)."""
        import time

        current = self._history_id()
        self.history_list.clear()
        for c in chats:
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(c["updated_at"]))
            mark = "● " if c["id"] == active else ""
            item = QListWidgetItem(f"{mark}{c['title']}\n{when} · повідомлень: {c['count']}")
            item.setData(Qt.ItemDataRole.UserRole, c["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, c["title"])
            item.setToolTip(c["title"] + ("\n(поточна розмова)" if mark else ""))
            self.history_list.addItem(item)
            if c["id"] == current:
                self.history_list.setCurrentItem(item)
        if self.history_list.count() and self.history_list.currentRow() < 0:
            self.history_list.setCurrentRow(0)
        self.history_empty.setVisible(not chats)
        self.history_list.setVisible(bool(chats))
        if chats:  # висота — під кількість чатів (до 380 px), без порожнього місця
            row_h = self.history_list.sizeHintForRow(0) + 4
            self.history_list.setFixedHeight(min(380, row_h * len(chats) + 8))
        self._history_buttons()
        self._fit()

    def _history_id(self) -> str | None:
        item = self.history_list.currentItem()
        if item is None:
            return None
        cid = item.data(Qt.ItemDataRole.UserRole)
        return None if cid is None else str(cid)  # сигнали несуть str — число загубилося б

    def _history_buttons(self) -> None:
        has = self._history_id() is not None
        for b in (self.btn_h_open, self.btn_h_rename, self.btn_h_export, self.btn_h_delete):
            b.setEnabled(has)

    def _history_action(self, action: str) -> None:
        cid = self._history_id()
        if cid is None:
            return
        if action == "open":
            self.history_open.emit(cid)
        elif action == "export":
            self.history_export.emit(cid)
        elif action == "rename":
            self.history_rename.emit(cid, self.history_list.currentItem().data(Qt.ItemDataRole.UserRole + 1))
        else:
            title = self.history_list.currentItem().data(Qt.ItemDataRole.UserRole + 1)
            self.ask(f"Видалити «{title}»? Це не можна скасувати.",
                     [("cancel", "Скасувати", "secondary"), ("delete", "Видалити", "danger")],
                     lambda key, _t: key == "delete" and self.history_delete.emit(cid))

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
        self.activity.setVisible(bool(text) and not self.history_shown)
        self._fit()

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
        self._fit()

    # ---------------- внутрішнє ----------------
    def _show_confirm(self) -> None:
        if not self._confirm_queue:
            self.confirm_box.hide()
            self._render()  # стрічка знову може бути вищою
        else:
            self.confirm_text.setPlainText(self._confirm_queue[0][1])
            self.confirm_text.moveCursor(QTextCursor.MoveOperation.Start)
            self.confirm_box.show()
            self._size_confirm_text()
            self._render()  # стрічка стає нижчою, щоб запит із кнопками вмістився на екрані
            self.btn_allow.setFocus()
        self._fit()

    def _screen_height(self) -> int:
        screen = self.screen() or QGuiApplication.primaryScreen()
        return screen.availableGeometry().height() if screen else 800

    def _size_confirm_text(self) -> None:
        """Висота поля з кодом: за вмістом, але не більше ~третини екрана (далі — прокрутка)."""
        lines = 0
        fm = self.confirm_text.fontMetrics()
        width = max(100, self.WIDTH - 2 * self.MARGIN - 60)
        for line in self.confirm_text.toPlainText().split("\n"):
            lines += max(1, -(-fm.horizontalAdvance(line) // width))  # з урахуванням переносу рядків
        content = lines * fm.lineSpacing() + 14
        cap = max(90, min(320, int(self._screen_height() * 0.33)))
        self.confirm_text.setFixedHeight(max(36, min(cap, content)))

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

    def set_show_tools(self, show: bool) -> None:
        """Показувати технічні рядки дій агента («🔧 read_file: …»). Файли 📎 і спогади 🧠 видно завжди."""
        self.show_tools = show
        self._render()

    def _render(self) -> None:
        parts = []
        for role, text in self.entries:
            if role == "tool" and not getattr(self, "show_tools", False) and not text.startswith(("📎", "🧠")):
                continue
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
        visible = bool(parts) and not self.history_shown
        self.view.setVisible(visible)
        if visible:
            doc = self.view.document()
            # ширина тексту = ширина картки мінус відступи й вертикальна прокрутка — щоб не було горизонтальної
            doc.setTextWidth(self.WIDTH - 2 * self.MARGIN - 28 - 18)
            doc_h = int(doc.size().height()) + 12
            # коли відкритий запит/підтвердження — стрічка нижча, щоб усе вмістилося на екрані
            cap = 420
            if self.ask_box.isVisible() or self.confirm_box.isVisible():
                cap = max(60, min(240, int(self._screen_height() * 0.2)))
            self.view.setFixedHeight(max(40, min(cap, doc_h)))
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())
        self._fit()
