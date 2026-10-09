"""Спливаюча панель: поле вводу задачі, хід виконання, відповідь, підтвердження дій."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QKeyEvent
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
QPushButton#flat { background: transparent; color: #5B6B82; padding: 2px 6px; font-size: 14px; }
QPushButton#flat:hover { color: #1D2740; }
QPushButton#allow { background: #5BE49B; color: #0E3B22; }
QPushButton#deny { background: #FFE1E1; color: #C92A2A; }
#confirm { background: #FFF6E9; border: 1px solid #FFC98A; border-radius: 12px; }
QLabel#confirmTitle { color: #B35C00; font-weight: 700; }
QLabel#confirmText { color: #3A2A10; font-family: monospace; font-size: 11px; }
"""


class InputBox(QPlainTextEdit):
    submitted = Signal()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(e)


class ChatPanel(QWidget):
    task_submitted = Signal(str)
    cancel_requested = Signal()
    reset_requested = Signal()
    confirm_answered = Signal(str, bool)
    closed = Signal()

    WIDTH = 400

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        self.setFixedWidth(self.WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
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
        self.btn_reset = QPushButton("⟲", objectName="flat", toolTip="Нова розмова")
        self.btn_reset.clicked.connect(self.reset_requested)
        self.btn_close = QPushButton("✕", objectName="flat", toolTip="Сховати")
        self.btn_close.clicked.connect(self.hide_panel)
        head.addWidget(self.btn_reset)
        head.addWidget(self.btn_close)
        lay.addLayout(head)

        # стрічка розмови
        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
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
        self.input.setPlaceholderText("Що зробити? (Enter — надіслати, Shift+Enter — новий рядок)")
        self.input.setFixedHeight(64)
        self.input.submitted.connect(self._submit)
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
    def show_panel(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def hide_panel(self) -> None:
        self.hide()
        self.closed.emit()

    def set_status(self, text: str, ok: bool, details: str = "") -> None:
        color = "#2F9E44" if ok else "#C92A2A"
        self.status.setText(f'<span style="color:{color}">●</span> {html.escape(text)}')
        self.status.setToolTip(details or text)
        self.adjustSize()

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
            doc.setTextWidth(max(self.view.viewport().width(), self.WIDTH - 64))
            doc_h = int(doc.size().height()) + 12
            self.view.setFixedHeight(max(40, min(420, doc_h)))
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())
        self.adjustSize()
