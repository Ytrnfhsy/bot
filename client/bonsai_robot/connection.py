"""WebSocket-з'єднання з сервером агента (QtWebSockets, з автоперепідключенням)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket


class AgentConnection(QObject):
    event = Signal(dict)
    connected = Signal()
    disconnected = Signal(str)

    def __init__(self, url: str, token: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.url = url
        self.token = token
        self.ws = QWebSocket()
        self.ws.connected.connect(self._on_connected)
        self.ws.disconnected.connect(self._on_disconnected)
        self.ws.textMessageReceived.connect(self._on_message)
        self.ws.errorOccurred.connect(self._on_error)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self.open)
        self._backoff = 1000
        self._ping = QTimer(self)
        self._ping.setInterval(25000)
        self._ping.timeout.connect(lambda: self.send({"type": "ping"}))
        self._last_error = ""

    @property
    def is_connected(self) -> bool:
        return self.ws.state() == QAbstractSocket.SocketState.ConnectedState

    def configure(self, url: str, token: str) -> None:
        self.url, self.token = url, token
        self._backoff = 1000
        self.ws.abort()
        self.open()

    def open(self) -> None:
        url = self.url
        if self.token:
            url += ("&" if "?" in url else "?") + "token=" + quote(self.token)
        self.ws.open(QUrl(url))

    def send(self, msg: dict[str, Any]) -> bool:
        if not self.is_connected:
            return False
        self.ws.sendTextMessage(json.dumps(msg, ensure_ascii=False))
        return True

    # ---- події сокета ----
    def _on_connected(self) -> None:
        self._backoff = 1000
        self._ping.start()
        self.connected.emit()

    def _on_disconnected(self) -> None:
        self._ping.stop()
        if self._retry.isActive():
            return
        reason = self.ws.closeReason() or self._last_error or "з'єднання втрачено"
        if self.ws.closeCode() == 4401:
            reason = "невірний токен"
        self.disconnected.emit(reason)
        self._retry.start(self._backoff)
        self._backoff = min(self._backoff * 2, 30000)

    def _on_error(self, _err) -> None:
        self._last_error = self.ws.errorString()
        if not self.is_connected and not self._retry.isActive():
            # disconnected() не завжди приходить, якщо підключення взагалі не встановилось
            self.disconnected.emit(self._last_error)
            self._retry.start(self._backoff)
            self._backoff = min(self._backoff * 2, 30000)

    def _on_message(self, text: str) -> None:
        try:
            self.event.emit(json.loads(text))
        except ValueError:
            pass
