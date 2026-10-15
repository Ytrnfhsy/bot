"""Надіслати повідомлення користувачу в Telegram."""

from __future__ import annotations

from . import Tool, ToolContext, ToolError
from ..telegram import TelegramBot, TelegramError, split_message


def chat_for(ctx: ToolContext) -> str | None:
    chat = ctx.storage.telegram_chat(ctx.owner)
    if not chat and ctx.owner == "owner":
        chat = ctx.config.telegram_chat_id or None
    return chat


async def send_telegram(ctx: ToolContext, text: str = "", path: str = "") -> str:
    if not text.strip() and not path:
        raise ToolError("Порожнє повідомлення")
    file = None
    if path:
        from .files import shared_file

        file = shared_file(ctx, path)[0]
    try:
        bot = TelegramBot(ctx.config.telegram_bot_token)
        chat = chat_for(ctx)
        if not chat:
            raise TelegramError(
                "Telegram цього користувача не прив'язано. Нехай на сервері виконає: "
                f"kru telegram link{'' if ctx.owner == 'owner' else ' --user ' + ctx.owner} і напише боту"
            )
        if file is not None:
            short = len(text) <= 1000
            if text.strip() and not short:
                await bot.send(chat, text)
            await bot.send_document(chat, file, text if short else "")
            return f"Надіслано в Telegram (з файлом {file.name})"
        await bot.send(chat, text)
        parts = len(split_message(text))
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
            "properties": {
                "text": {"type": "string", "description": "Текст повідомлення"},
                "path": {"type": "string", "description": "Необов'язково: файл з робочої теки, який додати документом"},
            },
            "required": ["text"],
        },
        func=send_telegram,
        summarize=lambda a: str(a.get("text", ""))[:300] + (f"\n📎 {a['path']}" if a.get("path") else ""),
    ),
]
