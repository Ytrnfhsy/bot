"""WebSocket-з'єднання з сервером агента (QtWebSockets, з автоперепідключенням)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWebSockets import QWebSocket


def normalize_url(url: str) -> str:
    """ws://host:8765 → ws://host:8765/ws; http(s):// → ws(s)://; без схеми → ws://"""
    url = url.strip()
    if "://" not in url:
        url = "ws://" + url
    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    path = parts.path if parts.path not in {"", "/"} else "/ws"
    return urlunsplit((scheme, parts.netloc, path, parts.query, ""))


def explain_error(raw: str) -> str:
    """Переклад технічних помилок Qt у зрозумілі підказки."""
    low = raw.lower()
    if "403" in low or "4401" in low or "unauthorized" in low:
        return "сервер відхилив підключення — перевір токен (BONSAI_TOKEN)"
    if "200 (ok)" in low or "status code: 200" in low:
        return ("за цією адресою не агент, а звичайний веб-сервер (схоже, llama-server) — "
                "вкажи порт агента: ws://ХОСТ:8765/ws і перевір, що запущено bonsai serve")
    if "404" in low:
        return "невірна адреса — має закінчуватися на /ws"
    if "refused" in low:
        return "сервер не відповідає на цьому порту — чи запущено bonsai serve і чи збігається порт?"
    if "host not found" in low or "not found" in low:
        return "хост не знайдено — перевір адресу (NetBird запущено?)"
    if "timed out" in low or "timeout" in low:
        return "час очікування вичерпано — сервер недоступний (мережа/файрвол/NetBird)"
    if "ssl" in low or "tls" in low:
        return "помилка TLS — для NetBird/LAN використовуй ws://, а не wss://"
    return raw or "з'єднання втрачено"


class AgentConnection(QObject):
    event = Signal(dict)
    connected = Signal()
    disconnected = Signal(str, str)  # (зрозуміла причина, технічний текст)

    def __init__(self, url: str, token: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.url = normalize_url(url)
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
        self.url, self.token = normalize_url(url), token
        self._backoff = 1000
        self.ws.abort()
        self.open()

    def open(self) -> None:
        self._was_connected = False
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
        self._was_connected = True
        self._ping.start()
        # connected() надсилаємо лише після "hello": при невірному токені сервер одразу закриє сокет

    def _on_disconnected(self) -> None:
        self._ping.stop()
        if not self._was_connected and not self.ws.closeReason():
            return  # невдала спроба — причину повідомить _on_error
        if self.ws.closeCode() == 4401:
            self._last_error = "4401 unauthorized"
        self._schedule_retry(self.ws.closeReason() or self._last_error)

    def _on_error(self, _err) -> None:
        self._last_error = self.ws.errorString()
        if not self.is_connected:
            # disconnected() не завжди приходить, якщо підключення взагалі не встановилось
            self._schedule_retry(self._last_error)

    def _schedule_retry(self, raw: str) -> None:
        if self._retry.isActive():
            return  # уже повідомили про цей обрив
        self.disconnected.emit(explain_error(raw), raw)
        self._last_error = ""
        self._retry.start(self._backoff)
        self._backoff = min(self._backoff * 2, 30000)

    def _on_message(self, text: str) -> None:
        try:
            ev = json.loads(text)
        except ValueError:
            return
        if ev.get("type") == "hello":
            self.connected.emit()
        self.event.emit(ev)
