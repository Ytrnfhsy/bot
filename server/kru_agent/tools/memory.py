"""Довготривала пам'ять агента."""

from __future__ import annotations

import asyncio
from datetime import datetime

from . import Tool, ToolContext, ToolError


def format_memories(items: list[dict]) -> str:
    return "\n".join(
        f"[#{m['id']} {datetime.fromtimestamp(m['created_at']):%Y-%m-%d}] {m['content']}"
        + (f"  (теги: {m['tags']})" if m.get("tags") else "")
        for m in items
    )


async def remember(ctx: ToolContext, content: str, tags: str = "") -> str:
    if not content.strip():
        raise ToolError("Порожній запис")
    mid = await asyncio.to_thread(ctx.storage.add_memory, content, tags, ctx.owner)
    return f"Запам'ятовано (#{mid})"


async def recall(ctx: ToolContext, query: str = "", limit: int = 8) -> str:
    items = await asyncio.to_thread(ctx.storage.search_memories, query, max(1, min(int(limit), 30)), ctx.owner)
    return format_memories(items) if items else "У пам'яті нічого не знайдено"


async def forget(ctx: ToolContext, memory_id: int) -> str:
    ok = await asyncio.to_thread(ctx.storage.delete_memory, int(memory_id), ctx.owner)
    return f"Запис #{memory_id} видалено" if ok else f"Запис #{memory_id} не знайдено"


TOOLS = [
    Tool(
        name="remember",
        description=(
            "Зберігає факт у довготривалу пам'ять (між сесіями): вподобання користувача, важливі дані, "
            "результати досліджень. Пиши самодостатні короткі факти."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Факт для запам'ятовування"},
                "tags": {"type": "string", "description": "Теги через кому"},
            },
            "required": ["content"],
        },
        func=remember,
    ),
    Tool(
        name="recall",
        description="Шукає у довготривалій пам'яті за ключовими словами (порожній query — останні записи).",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
        func=recall,
    ),
    Tool(
        name="forget",
        description="Видаляє запис із пам'яті за його номером.",
        parameters={
            "type": "object",
            "properties": {"memory_id": {"type": "integer"}},
            "required": ["memory_id"],
        },
        func=forget,
    ),
]
