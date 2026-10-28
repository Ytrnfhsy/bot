"""Навички (skills.py): прочитати потрібну, переглянути список, зберегти нову."""

from __future__ import annotations

from .. import skills as sk
from . import Tool, ToolContext, ToolError, preview, truncate


async def use_skill(ctx: ToolContext, name: str, file: str = "") -> str:
    skill = sk.all_skills(ctx.workspace).get(str(name).strip().lower())
    if skill is None:
        known = ", ".join(sk.all_skills(ctx.workspace)) or "немає жодної"
        raise ToolError(f"Навички {name!r} немає. Є: {known}")
    if file:  # скрипт чи шаблон із теки навички
        p = (skill.path / file).resolve()
        if skill.path.resolve() not in p.parents or not p.is_file():
            raise ToolError(f"У навичці {skill.name} немає файлу {file!r}. Є: {', '.join(skill.files()) or '—'}")
        return truncate(p.read_text("utf-8", "replace"), ctx.config.max_tool_output_chars)
    files = skill.files()
    where = (f"\n\nФайли навички ({'вбудована' if skill.builtin else skill.path}): {', '.join(files)} — "
             "прочитати: use_skill(name, file=…)" if files else "")
    return f"Навичка «{skill.name}» — {skill.description}\n\n{skill.body}{where}"


async def list_skills(ctx: ToolContext) -> str:
    skills = sk.all_skills(ctx.workspace)
    if not skills:
        return "Навичок ще немає"
    return "\n".join(f"- {s.name}{' (вбудована)' if s.builtin else ''}: {s.description}" for s in skills.values())


async def save_skill(ctx: ToolContext, name: str, description: str, instructions: str) -> str:
    name = str(name).strip().lower()
    if not sk.NAME_RE.match(name):
        raise ToolError("Назва — латиницею, малими літерами, цифри й дефіси (напр. weekly-report)")
    if not description.strip() or not instructions.strip():
        raise ToolError("Потрібні і опис (коли застосовувати), і сама інструкція")
    folder = sk.user_dir(ctx.workspace) / name
    target = ctx.resolve_path(str(folder.relative_to(ctx.workspace)) + "/" + sk.SKILL_FILE)
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(sk.render(name, description[: sk.MAX_DESCRIPTION], instructions), "utf-8")
    return (f"Навичку «{name}» {'оновлено' if existed else 'збережено'}: {target.relative_to(ctx.workspace)}. "
            "Вона з'явиться в списку навичок з наступної задачі.")


TOOLS = [
    Tool(
        name="use_skill",
        description=(
            "Відкриває навичку — збережену інструкцію, як робити певний тип задач (список навичок — у системному "
            "промпті). Виклич ПЕРЕД такою задачею й дій за інструкцією. file= — прочитати скрипт/шаблон навички."
        ),
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}, "file": {"type": "string", "description": "Необов'язково"}},
            "required": ["name"],
        },
        func=use_skill,
    ),
    Tool(
        name="list_skills",
        description="Список усіх навичок (назва й коли застосовувати).",
        parameters={"type": "object", "properties": {}},
        func=list_skills,
    ),
    Tool(
        name="save_skill",
        description=(
            "Зберігає нову навичку (або оновлює наявну): як робити певний повторюваний тип задач. Використовуй, коли "
            "користувач просить запам'ятати спосіб/формат/порядок дій. description — один рядок, КОЛИ її застосовувати; "
            "instructions — покрокова інструкція в Markdown (джерела, кроки, формат результату, типові помилки)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "латиницею: weekly-report"},
                "description": {"type": "string"},
                "instructions": {"type": "string"},
            },
            "required": ["name", "description", "instructions"],
        },
        func=save_skill,
        dangerous=True,  # потрапляє в системний промпт кожної наступної задачі — користувач має бачити, що саме
        summarize=lambda a: f"Зберегти навичку «{a.get('name', '')}»: {a.get('description', '')}\n\n"
                            + preview(str(a.get("instructions", "")), 4000),
    ),
]
