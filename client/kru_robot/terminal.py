"""«Термінал» хакерської теми: код, який агент саме пише, — наживо, символ за символом.

Сервер пересилає аргументи інструментів з кодом шматками JSON, поки модель їх генерує (подія tool_stream):
  {"path": "app.py", "content": "import os\\nprint(…   ← обірвано посеред рядка
partial_field дістає з такого незакінченого JSON значення потрібного поля (content, code, command…),
а CodeTerminal дописує в кінець лише нове — без перемальовування всього тексту.
"""

from __future__ import annotations

import json
import re

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

# інструмент → (поле з кодом, поле-підпис, як показати)
CODE_FIELDS = {
    "write_file": ("content", "path", "✎ {}"),
    "edit_file": ("new_text", "path", "✎ {} (зміна)"),
    "run_python": ("code", "", "▶ python"),
    "run_shell": ("command", "", "$"),
    "ssh_run": ("command", "host", "$ ssh {}"),
}
MAX_LINES = 600  # старіше прокручується геть — термінал не росте безмежно


def partial_field(raw: str, key: str) -> tuple[str, bool]:
    """Значення рядкового поля key з (можливо, незакінченого) JSON → (текст досі, чи поле вже закрите)."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', raw)
    if not m:
        return "", False
    i, n, body = m.end(), len(raw), []
    while i < n:
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= n:
                break  # «\» в самому кінці — продовження ще не прийшло
            if raw[i + 1] == "u":
                if i + 6 > n:
                    break  # \uXXXX ще не повний
                body.append(raw[i:i + 6])
                i += 6
                continue
            body.append(raw[i:i + 2])
            i += 2
            continue
        if ch == '"':
            return _decode("".join(body)), True
        body.append(ch)
        i += 1
    return _decode("".join(body)), False


def _decode(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        # половина сурогатної пари емодзі тощо — те, що вже точно відомо
        return json.loads(f'"{s.rsplit(chr(92) + "u", 1)[0]}"') if "\\u" in s else s


class CodeTerminal(QFrame):
    """Чорне вікно із зеленим моноширинним текстом над полем вводу. Видно лише в хакерській темі."""

    closed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent, objectName="terminal")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 8)
        lay.setSpacing(2)
        head = QHBoxLayout()
        self.title = QLabel("▌ код наживо", objectName="terminalTitle")
        close = QPushButton("✕", objectName="flat", toolTip="Сховати термінал (з'явиться знову з новим кодом)")
        close.setFixedSize(22, 22)
        close.clicked.connect(self._close)
        head.addWidget(self.title, 1)
        head.addWidget(close)
        lay.addLayout(head)
        self.view = QPlainTextEdit(objectName="terminalView", readOnly=True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setMaximumBlockCount(MAX_LINES)
        font = QFont("Monospace", 9)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.view.setFont(font)
        self.view.setFixedHeight(170)
        lay.addWidget(self.view)
        self.hide()
        self._calls: dict[int, dict] = {}  # index виклику → {raw, name, shown}
        self.enabled = False  # хакерська тема

    def _close(self) -> None:
        self.hide()
        self.closed.emit()

    def reset(self) -> None:
        """Нова задача — новий виклик інструментів (index знову з 0)."""
        self._calls.clear()

    def stream(self, index: int, name: str, delta: str) -> None:
        if not self.enabled or name not in CODE_FIELDS:
            return
        call = self._calls.get(index)
        if call is None or call["name"] != name or call.get("done"):
            call = self._calls[index] = {"raw": "", "name": name, "shown": "", "header": False}
        call["raw"] += delta
        field, label_key, label = CODE_FIELDS[name]
        if not call["header"]:
            caption = partial_field(call["raw"], label_key) if label_key else ("", True)
            if label_key and not caption[1] and field not in call["raw"]:
                return  # ще чекаємо назву файлу — підпис без неї вийшов би неповним
            head = label.format(caption[0])
            # команда — у тому ж рядку, що й «$», код файлу — з нового рядка під підписом
            self._append(("\n" if self.view.toPlainText() else "") + head + (" " if head.startswith("$") else "\n"))
            call["header"] = True
            self.show()
        text, done = partial_field(call["raw"], field)
        if text.startswith(call["shown"]):
            self._append(text[len(call["shown"]):])
        else:  # модель «передумала» (рідко) — показуємо як є
            self._append("\n" + text)
        call["shown"] = text
        if done:
            call["done"] = True
            if not text.endswith("\n"):
                self._append("\n")

    def whole(self, index: int, name: str, args: dict) -> None:
        """Виклик інструмента цілим (tool_call): якщо шматками його не було (Ollama, JSON-режим) — показати весь."""
        if not self.enabled or name not in CODE_FIELDS or (index in self._calls and self._calls[index]["name"] == name):
            return
        import json as _json

        self.stream(index, name, _json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False))

    def output(self, name: str, ok: bool, text: str) -> None:
        """Результат запуску (run_shell / run_python / …) — під кодом."""
        if not self.enabled or name not in CODE_FIELDS or self.isHidden():
            return
        if name in {"write_file", "edit_file"}:
            self._append(("✓ " if ok else "✗ ") + " ".join(text.split())[:160] + "\n")
            return
        self._append(("── вивід ──\n" if ok else "── помилка ──\n") + text.rstrip() + "\n")

    def _append(self, text: str) -> None:
        if not text:
            return
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(text)
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def plain(self) -> str:
        return self.view.toPlainText()

