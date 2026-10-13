"""Довготривала пам'ять агента."""

from __future__ import annotations

import asyncio
from datetime import datetime

from . import Tool, ToolContext, ToolError

DUPLICATE = 0.95  # такий самий факт уже є — не дублюємо
RELATED = 0.75  # схожий запис: можливо, застарів — модель вирішує, чи забути його


def format_memories(items: list[dict]) -> str:
    return "\n".join(
        f"[#{m['id']} {datetime.fromtimestamp(m['created_at']):%Y-%m-%d}] {m['content']}"
        + (f"  (теги: {m['tags']})" if m.get("tags") else "")
        for m in items
    )


async def search(ctx: ToolContext, query: str, limit: int) -> list[dict]:
    if ctx.memory is not None:
        return await ctx.memory.search(query, ctx.owner, limit)
    return await asyncio.to_thread(ctx.storage.search_memories, query, limit, ctx.owner)


async def remember(ctx: ToolContext, content: str, tags: str = "") -> str:
    if not content.strip():
        raise ToolError("Порожній запис")
    mid = await asyncio.to_thread(ctx.storage.add_memory, content, tags, ctx.owner)
    vector = await ctx.memory.index(mid, content) if ctx.memory is not None else None
    if vector is None:
        return f"Запам'ятовано (#{mid})"
    near = await ctx.memory.similar(vector, ctx.owner, limit=3, exclude=mid)
    if near and near[0][0] >= DUPLICATE:
        await asyncio.to_thread(ctx.storage.delete_memory, mid, ctx.owner)
        return f"Це вже є в пам'яті: #{near[0][1]['id']} {near[0][1]['content']}"
    related = [row for score, row in near if score >= RELATED]
    if related:
        return (
            f"Запам'ятовано (#{mid}). Схожі записи — якщо якийсь застарів або суперечить новому, видали його forget:\n"
            + format_memories(related)
        )
    return f"Запам'ятовано (#{mid})"


async def recall(ctx: ToolContext, query: str = "", limit: int = 8) -> str:
    items = await search(ctx, query, max(1, min(int(limit), 30)))
    return format_memories(items) if items else "У пам'яті нічого не знайдено"


async def forget(ctx: ToolContext, memory_id: int) -> str:
    ok = await asyncio.to_thread(ctx.storage.delete_memory, int(memory_id), ctx.owner)
    return f"Запис #{memory_id} видалено" if ok else f"Запис #{memory_id} не знайдено"


TOOLS = [
    Tool(
        name="remember",
        description=(
            "Зберігає факт у довготривалу пам'ять (між чатами): про користувача, його вподобання, людей, плани, "
            "проєкти, рішення, важливі результати. Один короткий самодостатній факт на виклик."
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
        summarize=lambda a: str(a.get("content", "")),
    ),
    Tool(
        name="recall",
        description=(
            "Шукає у довготривалій пам'яті за змістом і ключовими словами "
            "(можна питати своїми словами, напр. «що користувач казав про відпустку»; порожній query — останні записи)."
        ),
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
        description="Видаляє запис із пам'яті за його номером (напр. застарілий чи хибний).",
        parameters={
            "type": "object",
            "properties": {"memory_id": {"type": "integer"}},
            "required": ["memory_id"],
        },
        func=forget,
    ),
]
