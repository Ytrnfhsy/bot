"""Надіслати повідомлення користувачу в Telegram."""

from __future__ import annotations

from . import Tool, ToolContext, ToolError
from ..telegram import TelegramBot, TelegramError


def chat_for(ctx: ToolContext) -> str | None:
    chat = ctx.storage.telegram_chat(ctx.owner)
    if not chat and ctx.owner == "owner":
        chat = ctx.config.telegram_chat_id or None
    return chat


async def send_telegram(ctx: ToolContext, text: str) -> str:
    if not text.strip():
        raise ToolError("Порожнє повідомлення")
    try:
        bot = TelegramBot(ctx.config.telegram_bot_token)
        chat = chat_for(ctx)
        if not chat:
            raise TelegramError(
                "Telegram цього користувача не прив'язано. Нехай на сервері виконає: "
                f"kru telegram link{'' if ctx.owner == 'owner' else ' --user ' + ctx.owner} і напише боту"
            )
        parts = await bot.send(chat, text)
    except TelegramError as e:
        raise ToolError(str(e)) from e
    return "Надіслано в Telegram" + (f" ({parts} повідомлення)" if parts > 1 else "")


TOOLS = [
    Tool(
        name="send_telegram",
        description=(
            "Надсилає повідомлення користувачу в його Telegram (наприклад, результат пошуку чи нагадування). "
            "Пиши готовий текст для людини: коротко, по суті, з посиланнями; без Markdown-розмітки."
        ),
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Текст повідомлення"}},
            "required": ["text"],
        },
        func=send_telegram,
        summarize=lambda a: str(a.get("text", ""))[:300],
    ),
]
