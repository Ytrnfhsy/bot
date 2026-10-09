"""Вікно «Збережені чати»: відкрити, експортувати в Markdown, видалити."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


class ChatsDialog(QDialog):
    open_requested = Signal(int)
    export_requested = Signal(int)
    delete_requested = Signal(int)

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Збережені чати")
        self.resize(520, 420)
        lay = QVBoxLayout(self)
        self.empty = QLabel("Збережених чатів поки немає.\nНатисни 💾 у панелі робота або Ctrl+S, щоб зберегти розмову.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color: #5B6B82; padding: 24px;")
        lay.addWidget(self.empty)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self._emit(self.open_requested))
        self.list.currentItemChanged.connect(lambda *_: self._update_buttons())
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_open = QPushButton("📂 Відкрити")
        self.btn_export = QPushButton("📤 Експорт .md")
        self.btn_delete = QPushButton("🗑 Видалити")
        close = QPushButton("Закрити")
        self.btn_open.clicked.connect(lambda: self._emit(self.open_requested))
        self.btn_export.clicked.connect(lambda: self._emit(self.export_requested))
        self.btn_delete.clicked.connect(self._confirm_delete)
        close.clicked.connect(self.close)
        for b in (self.btn_open, self.btn_export, self.btn_delete):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        self.set_chats([])

    def set_chats(self, chats: list[dict]) -> None:
        current = self._current_id()
        self.list.clear()
        for c in chats:
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(c["updated_at"]))
            item = QListWidgetItem(f"{c['title']}\n    {when} · повідомлень: {c['count']}")
            item.setData(Qt.ItemDataRole.UserRole, c["id"])
            self.list.addItem(item)
            if c["id"] == current:
                self.list.setCurrentItem(item)
        if self.list.count() and self.list.currentRow() < 0:
            self.list.setCurrentRow(0)
        self.empty.setVisible(not chats)
        self.list.setVisible(bool(chats))
        self._update_buttons()

    def _current_id(self) -> int | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_buttons(self) -> None:
        has = self._current_id() is not None
        for b in (self.btn_open, self.btn_export, self.btn_delete):
            b.setEnabled(has)

    def _emit(self, signal: Signal) -> None:
        cid = self._current_id()
        if cid is not None:
            signal.emit(cid)
            if signal is self.open_requested:
                self.close()

    def _confirm_delete(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        title = item.text().split("\n")[0]
        if QMessageBox.question(self, "Видалити чат", f"Видалити «{title}»? Це не можна скасувати.") == QMessageBox.StandardButton.Yes:
            self._emit(self.delete_requested)
