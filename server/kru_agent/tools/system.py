"""Дрібні системні інструменти."""

from __future__ import annotations

from datetime import datetime

from . import Tool, ToolContext


async def current_datetime(ctx: ToolContext) -> str:
    now = datetime.now().astimezone()
    return now.strftime("%Y-%m-%d %H:%M:%S %Z (%A)")


TOOLS = [
    Tool(
        name="current_datetime",
        description="Повертає поточні дату та час сервера.",
        parameters={"type": "object", "properties": {}},
        func=current_datetime,
    ),
]
