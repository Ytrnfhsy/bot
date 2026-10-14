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


class TelegramBot:
    def __init__(self, token: str, timeout: float = 30) -> None:
        if not token:
            raise TelegramError(
                "Telegram не налаштовано: створи бота в @BotFather і додай KRU_TELEGRAM_BOT_TOKEN у ~/.config/kru.env, "
                "потім на сервері: kru telegram link"
            )
        self.base = f"{API}/bot{token}"
        self.timeout = timeout

    async def _call(self, method: str, **params: Any) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(f"{self.base}/{method}", json=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"Telegram недоступний: {e}") from e
        if not data.get("ok"):
            raise TelegramError(f"Telegram: {data.get('description') or r.status_code}")
        return data.get("result")

    async def send(self, chat_id: str, text: str) -> int:
        parts = split_message(text)
        if not parts:
            raise TelegramError("Порожнє повідомлення")
        for part in parts:
            # без parse_mode: довільний текст моделі не зламає розмітку Telegram
            await self._call("sendMessage", chat_id=chat_id, text=part, disable_web_page_preview=True)
        return len(parts)

    async def me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return await self._call("getUpdates", **params)
