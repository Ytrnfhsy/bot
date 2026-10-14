"""План великої задачі (чек-лист у панелі) і доручення підзадач субагенту."""

from __future__ import annotations

from typing import Any

from . import Tool, ToolContext, ToolError

STATUSES = {"pending", "in_progress", "done", "skipped"}
MARKS = {"pending": "☐", "in_progress": "⏳", "done": "✅", "skipped": "➖"}
SUBAGENT_DENIED = frozenset({"update_plan", "mark_step", "delegate"})
PLAN_TOOLS = frozenset({"update_plan", "mark_step"})  # субагент не планує й не делегує далі


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


def apply_marks(plan: list[dict[str, str]], steps: list[int], status: str) -> list[dict[str, str]]:
    """Позначити пункти (нумерація з 1); після «done» наступний невиконаний стає in_progress."""
    plan = [dict(s) for s in plan]
    bad = [n for n in steps if not 1 <= n <= len(plan)]
    if bad:
        raise ToolError(f"Немає пункту {bad[0]}: у плані {len(plan)} пунктів")
    for n in steps:
        plan[n - 1]["status"] = status
    if status in {"done", "skipped"} and not any(s["status"] == "in_progress" for s in plan):
        for s in plan[max(steps):] + plan[: max(steps)]:
            if s["status"] == "pending":
                s["status"] = "in_progress"
                break
    return plan


async def mark_step(ctx: ToolContext, step: Any, status: str = "done") -> str:
    if ctx.plan_hook is None or ctx.plan_get is None:
        raise ToolError("План недоступний у цьому режимі")
    plan = ctx.plan_get()
    if not plan:
        raise ToolError("Плану ще немає — спершу склади його через update_plan")
    status = str(status or "done").lower()
    if status not in STATUSES:
        raise ToolError(f"status: {', '.join(sorted(STATUSES))}")
    raw = step if isinstance(step, list) else [step]
    try:
        steps = sorted({int(x) for x in raw})
    except (TypeError, ValueError) as e:
        raise ToolError("step — номер пункту (або список номерів), починаючи з 1") from e
    if not steps:
        raise ToolError("Вкажи номер пункту")
    plan = apply_marks(plan, steps, status)
    await ctx.plan_hook(plan)
    left = sum(1 for s in plan if s["status"] in {"pending", "in_progress"})
    current = next((f"{i}. {s['title']}" for i, s in enumerate(plan, 1) if s["status"] == "in_progress"), "")
    if not left:
        return "Позначено. Усі кроки плану виконано — напиши підсумкову відповідь."
    return f"Позначено. Залишилось: {left}." + (f" Зараз: {current}" if current else "")


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
        name="mark_step",
        description=(
            "Швидко позначає пункт(и) плану: step — номер (з 1) або список номерів, status — done (типово), "
            "in_progress, skipped. Після done наступний пункт сам стає in_progress. Викликай ОДРАЗУ, щойно "
            "закінчив пункт плану, — користувач бачить чек-лист."
        ),
        parameters={
            "type": "object",
            "properties": {
                "step": {"description": "Номер пункту або список номерів",
                         "anyOf": [{"type": "integer"}, {"type": "array", "items": {"type": "integer"}}]},
                "status": {"type": "string", "enum": sorted(STATUSES)},
            },
            "required": ["step"],
        },
        func=mark_step,
        summarize=lambda a: f"{a.get('step')} → {a.get('status', 'done')}",
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
