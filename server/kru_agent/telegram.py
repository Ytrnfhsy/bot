"""Telegram: надсилання повідомлень користувачу через його бота (@BotFather → KRU_TELEGRAM_BOT_TOKEN).

Прив'язка чату: `kru telegram link` — користувач пише боту будь-що, і його chat_id зберігається.
"""

from __future__ import annotations

from typing import Any

import httpx

API = "https://api.telegram.org"
LIMIT = 4000  # Telegram приймає до 4096 символів в одному повідомленні


class TelegramError(Exception):
    pass


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Ділить довгий текст на частини — по абзацах/рядках, а не посеред слова."""
    text = text.strip()
    parts: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


class TelegramBot:
    def __init__(self, token: str, timeout: float = 30) -> None:
        if not token:
            raise TelegramError(
                "Telegram не налаштовано: створи бота в @BotFather і додай KRU_TELEGRAM_BOT_TOKEN у ~/.config/kru.env, "
                "потім на сервері: kru telegram link"
            )
        self.base = f"{API}/bot{token}"
        self.timeout = timeout  # для getUpdates із long polling додається його власний timeout

    async def _call(self, method: str, **params: Any) -> Any:
        try:
            wait = self.timeout + float(params.get("timeout") or 0) if method == "getUpdates" else self.timeout
            async with httpx.AsyncClient(timeout=wait) as client:
                r = await client.post(f"{self.base}/{method}", json=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"Telegram недоступний: {e}") from e
        if not data.get("ok"):
            raise TelegramError(f"Telegram: {data.get('description') or r.status_code}")
        return data.get("result")

    async def send(self, chat_id: str, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> int:
        """Текст (довгий — кількома повідомленнями). buttons — рядки кнопок [(напис, callback_data)] під останнім.
        Повертає message_id останнього повідомлення."""
        parts = split_message(text)
        if not parts:
            raise TelegramError("Порожнє повідомлення")
        msg_id = 0
        for i, part in enumerate(parts):
            # без parse_mode: довільний текст моделі не зламає розмітку Telegram
            params: dict[str, Any] = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if buttons and i == len(parts) - 1:
                params["reply_markup"] = keyboard(buttons)
            res = await self._call("sendMessage", **params)
            msg_id = int((res or {}).get("message_id") or 0)
        return msg_id

    async def edit(self, chat_id: str, message_id: int, text: str) -> None:
        """Змінити текст повідомлення (і прибрати кнопки)."""
        await self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:LIMIT],
                         disable_web_page_preview=True)

    async def delete(self, chat_id: str, message_id: int) -> None:
        await self._call("deleteMessage", chat_id=chat_id, message_id=message_id)

    async def typing(self, chat_id: str) -> None:
        await self._call("sendChatAction", chat_id=chat_id, action="typing")

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def download(self, file_id: str, max_bytes: int = 20 * 1024 * 1024) -> tuple[bytes, str]:
        """Файл, надісланий боту: (вміст, шлях у Telegram — з нього видно розширення)."""
        info = await self._call("getFile", file_id=file_id)
        path = (info or {}).get("file_path") or ""
        if not path:
            raise TelegramError("Telegram не віддав файл (буває для файлів понад 20 МБ)")
        if int((info or {}).get("file_size") or 0) > max_bytes:
            raise TelegramError("файл завеликий (понад 20 МБ)")
        url = self.base.replace("/bot", "/file/bot", 1) + "/" + path
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise TelegramError(f"не вдалося завантажити файл: {e}") from e
        return r.content, path

    async def me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return await self._call("getUpdates", **params)
