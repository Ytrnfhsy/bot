"""Навички: інструкції «як робити X» у файлах SKILL.md, які агент підвантажує, коли задача підходить.

Навичка — тека з SKILL.md (і, за потреби, скриптами чи шаблонами поруч):

    ---
    name: nist-csp-exam
    description: Підготовка до іспиту NIST Cybersecurity Professional — пояснення, тести, повторення слабких тем
    ---
    (інструкція звичайною мовою)

Звідки беруться:
  * вбудовані — kru_agent/builtin_skills/<назва>/ (ідуть разом з агентом);
  * свої — <робоча тека користувача>/skills/<назва>/ (додає сам користувач, агент через save_skill
    чи `kru skill add`); своя навичка з тією ж назвою замінює вбудовану.

У системному промпті — лише назва й опис кожної (кілька десятків токенів), повний текст модель бере
інструментом use_skill, коли він потрібен.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

BUILTIN_DIR = Path(__file__).with_name("builtin_skills")
SKILL_FILE = "SKILL.md"
USER_DIR = "skills"  # у робочій теці користувача
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
MAX_DESCRIPTION = 300
MAX_LISTED = 60  # стільки навичок потрапляє в промпт (решта — лише через list_skills)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path  # тека навички
    builtin: bool

    def files(self) -> list[str]:
        """Інші файли навички (скрипти, шаблони) — відносні шляхи."""
        return sorted(p.relative_to(self.path).as_posix() for p in self.path.rglob("*")
                      if p.is_file() and p.name != SKILL_FILE and not p.name.startswith("."))


def parse(text: str) -> tuple[dict[str, str], str]:
    """--- name: … / description: … --- + тіло. Без YAML-бібліотеки: прості рядки «ключ: значення»."""
    meta: dict[str, str] = {}
    body = text.lstrip("﻿")
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            for line in body[3:end].splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip().lower()] = value.strip().strip("\"'")
            body = body[end + 4:]
    return meta, body.strip()


def render(name: str, description: str, body: str) -> str:
    description = " ".join(description.split())
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n"


def _load(folder: Path, builtin: bool) -> Skill | None:
    f = folder / SKILL_FILE
    if not f.is_file() or f.is_symlink():
        return None
    try:
        meta, body = parse(f.read_text("utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    name = meta.get("name") or folder.name
    if not NAME_RE.match(name) or not body:
        return None
    description = " ".join(meta.get("description", "").split())[:MAX_DESCRIPTION] or body.splitlines()[0][:120]
    return Skill(name, description, body, folder, builtin)


def user_dir(workspace: Path) -> Path:
    return Path(workspace) / USER_DIR


def all_skills(workspace: Path | None) -> dict[str, Skill]:
    """Назва → навичка: вбудовані, поверх них — свої (своя замінює вбудовану з тією ж назвою)."""
    found: dict[str, Skill] = {}
    roots = [(BUILTIN_DIR, True)] + ([(user_dir(workspace), False)] if workspace else [])
    for root, builtin in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for folder in sorted(root.iterdir()):
            if folder.is_dir() and not folder.is_symlink() and (skill := _load(folder, builtin)):
                found[skill.name] = skill
    return found


def prompt_section(workspace: Path | None) -> str:
    skills = list(all_skills(workspace).values())[:MAX_LISTED]
    if not skills:
        return ""
    lines = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    return (
        "\nSkills — saved know-how for specific kinds of tasks. When a task matches one, FIRST call "
        "use_skill(name) and follow its instructions (they override your usual approach for that task):\n"
        f"{lines}\n"
        "When the user asks you to remember HOW to do something recurring (a workflow, a format, a checklist), "
        "save it with save_skill.\n"
    )
