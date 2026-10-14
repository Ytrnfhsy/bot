"""Завдання на конкретний час: створити, переглянути, скасувати."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from . import Tool, ToolContext, ToolError
from ..scheduler import REPEATS, describe, parse_when

REPEAT_ALIASES = {
    "none": "", "once": "", "no": "", "": "",
    "hourly": "hourly", "hour": "hourly",
    "daily": "daily", "day": "daily", "everyday": "daily",
    "weekdays": "weekdays", "workdays": "weekdays",
    "weekly": "weekly", "week": "weekly",
}


async def schedule_task(ctx: ToolContext, task: str, when: str, repeat: str = "") -> str:
    if not task.strip():
        raise ToolError("Порожнє завдання")
    rep = REPEAT_ALIASES.get(str(repeat or "").strip().lower())
    if rep is None:
        raise ToolError(f"repeat має бути одним із: {', '.join(k or 'none' for k in REPEATS)}")
    # час — у поясі користувача (не сервера): «о 14:00» означає 14:00 за його годинником
    now = datetime.now(ctx.tz).replace(tzinfo=None)
    try:
        at = parse_when(when, now)
    except ValueError as e:
        raise ToolError(str(e)) from e
    ts = at.replace(tzinfo=ctx.tz).timestamp() if ctx.tz else at.timestamp()
    if ts < time.time() - 60:
        raise ToolError(f"Час {at:%Y-%m-%d %H:%M} уже минув (зараз {now:%Y-%m-%d %H:%M}) — вкажи майбутній")
    sid = await asyncio.to_thread(ctx.storage.add_schedule, ctx.owner, task, ts, rep, ctx.tz_name)
    zone = f" ({ctx.tz_name})" if ctx.tz_name else ""
    return (
        f"Заплановано #{sid}: {at:%Y-%m-%d %H:%M (%A)}{zone}, {REPEATS[rep]}.\n"
        "Завдання виконається автоматично в окремому чаті «⏰ …»; персонаж покаже сповіщення."
    )


async def list_scheduled(ctx: ToolContext, include_done: bool = False) -> str:
    items = await asyncio.to_thread(ctx.storage.list_schedules, ctx.owner, bool(include_done))
    if not items:
        return "Запланованих завдань немає"
    return "\n".join(describe(i) + ("" if i["enabled"] else " [неактивне]") for i in items)


async def cancel_scheduled(ctx: ToolContext, schedule_id: int) -> str:
    ok = await asyncio.to_thread(ctx.storage.cancel_schedule, int(schedule_id), ctx.owner)
    return f"Завдання #{schedule_id} скасовано" if ok else f"Активного завдання #{schedule_id} не знайдено"


TOOLS = [
    Tool(
        name="schedule_task",
        description=(
            "Планує завдання на конкретний час (місцевий час користувача — той, що в системному промпті): у цей "
            "момент агент сам виконає task — "
            "наприклад «знайди курс долара і надішли мені в Telegram». task пиши повністю й самодостатньо: при "
            "виконанні не буде цієї розмови. when: «2026-10-14 18:00», «18:00» (найближче), «+30m», «+2h», «+1d». "
            "repeat: none | hourly | daily | weekdays | weekly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Що зробити (повний самодостатній опис)"},
                "when": {"type": "string", "description": "Коли: YYYY-MM-DD HH:MM, HH:MM або +30m/+2h/+1d"},
                "repeat": {"type": "string", "enum": ["none", "hourly", "daily", "weekdays", "weekly"]},
            },
            "required": ["task", "when"],
        },
        func=schedule_task,
        summarize=lambda a: f"{a.get('when')} ({a.get('repeat') or 'одноразово'}): {a.get('task')}",
    ),
    Tool(
        name="list_scheduled",
        description="Показує заплановані завдання користувача (номер, час, повтор, останній запуск).",
        parameters={
            "type": "object",
            "properties": {"include_done": {"type": "boolean", "description": "Показати й виконані/скасовані"}},
        },
        func=list_scheduled,
    ),
    Tool(
        name="cancel_scheduled",
        description="Скасовує заплановане завдання за номером (дізнатися номер — list_scheduled).",
        parameters={
            "type": "object",
            "properties": {"schedule_id": {"type": "integer"}},
            "required": ["schedule_id"],
        },
        func=cancel_scheduled,
    ),
]
