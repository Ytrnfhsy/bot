"""План великої задачі (чек-лист у панелі) і доручення підзадач субагенту."""

from __future__ import annotations

from typing import Any

from . import Tool, ToolContext, ToolError

STATUSES = {"pending", "in_progress", "done", "skipped"}
MARKS = {"pending": "☐", "in_progress": "⏳", "done": "✅", "skipped": "➖"}
SUBAGENT_DENIED = frozenset({"update_plan", "delegate"})  # субагент не планує й не делегує далі


def normalize_plan(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ToolError("steps — непорожній список кроків [{title, status}]")
    steps = []
    for item in raw[:20]:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or item.get("step") or "").split())[:160]
        status = str(item.get("status") or "pending").lower()
        if title:
            steps.append({"title": title, "status": status if status in STATUSES else "pending"})
    if not steps:
        raise ToolError("У плані немає жодного кроку з назвою")
    return steps


def format_plan(steps: list[dict[str, str]]) -> str:
    return "\n".join(f"{MARKS.get(s['status'], '☐')} {i}. {s['title']}" for i, s in enumerate(steps, 1))


async def update_plan(ctx: ToolContext, steps: list) -> str:
    if ctx.plan_hook is None:
        raise ToolError("План недоступний у цьому режимі")
    plan = normalize_plan(steps)
    await ctx.plan_hook(plan)
    left = sum(1 for s in plan if s["status"] in {"pending", "in_progress"})
    tail = "Усі кроки виконано — напиши підсумкову відповідь." if not left else f"Залишилось кроків: {left}."
    return f"План оновлено:\n{format_plan(plan)}\n{tail}"


async def delegate(ctx: ToolContext, task: str, context: str = "") -> str:
    if ctx.delegate_hook is None:
        raise ToolError("Субагент недоступний у цьому режимі")
    if not task.strip():
        raise ToolError("Порожня підзадача")
    return await ctx.delegate_hook(task.strip(), context.strip())


TOOLS = [
    Tool(
        name="update_plan",
        description=(
            "Чек-лист великої задачі, який бачить користувач. Для задач із 3+ різними кроками: спершу склади план "
            "(усі кроки pending), далі перед кожним кроком став йому in_progress, після — done. Щоразу передавай "
            "ВЕСЬ список кроків. status: pending | in_progress | done | skipped."
        ),
        parameters={
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Короткий опис кроку"},
                            "status": {"type": "string", "enum": sorted(STATUSES)},
                        },
                        "required": ["title", "status"],
                    },
                }
            },
            "required": ["steps"],
        },
        func=update_plan,
        summarize=lambda a: "\n".join(
            f"{MARKS.get(str(s.get('status')), '☐')} {s.get('title')}" for s in a.get("steps", []) if isinstance(s, dict)
        ),
    ),
    Tool(
        name="delegate",
        description=(
            "Доручає самодостатню підзадачу субагенту з чистим контекстом (ті самі інструменти) і повертає лише його "
            "результат. Корисно, коли підзадача потребує багато читання/пошуку (кілька файлів, сторінок), а тобі "
            "потрібен тільки висновок — так твоя розмова не засмічується сирими даними. Субагент не бачить цієї "
            "розмови: опиши задачу повністю і передай потрібні деталі в context."
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Що саме зробити і що повернути (формат результату)"},
                "context": {"type": "string", "description": "Потрібні факти з розмови: шляхи, вимоги, обмеження"},
            },
            "required": ["task"],
        },
        func=delegate,
        summarize=lambda a: str(a.get("task", "")),
    ),
]
