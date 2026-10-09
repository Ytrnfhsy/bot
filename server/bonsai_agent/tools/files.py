"""Робота з файлами у робочій директорії агента."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re

from . import Tool, ToolContext, ToolError, truncate

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".bonsai_tmp"}


async def read_file(ctx: ToolContext, path: str, start_line: int = 1, max_lines: int = 400) -> str:
    p = ctx.resolve_path(path)
    if not p.is_file():
        raise ToolError(f"Файл не знайдено: {path}")
    if p.stat().st_size > 20 * 1024 * 1024:
        raise ToolError("Файл завеликий (>20 МБ)")
    data = p.read_bytes()
    if b"\x00" in data[:4096]:
        raise ToolError("Схоже, це бінарний файл — прочитати як текст неможливо")
    lines = data.decode("utf-8", "replace").splitlines()
    start = max(1, int(start_line))
    end = min(len(lines), start - 1 + max(1, int(max_lines)))
    body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))
    more = f"\n…ще {len(lines) - end} рядків (start_line={end + 1})" if end < len(lines) else ""
    return truncate(f"{p} (рядки {start}-{end} з {len(lines)})\n{body}{more}", ctx.config.max_tool_output_chars)


async def write_file(ctx: ToolContext, path: str, content: str, append: bool = False) -> str:
    p = ctx.resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    return f"{'Дописано' if append else 'Записано'} {len(content)} символів у {p}"


async def edit_file(ctx: ToolContext, path: str, old_text: str, new_text: str) -> str:
    p = ctx.resolve_path(path)
    if not p.is_file():
        raise ToolError(f"Файл не знайдено: {path}")
    text = p.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        raise ToolError("old_text не знайдено у файлі — спершу прочитай файл через read_file")
    if count > 1:
        raise ToolError(f"old_text зустрічається {count} разів — додай більше контексту, щоб він був унікальним")
    p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"Файл {p} змінено"


async def list_dir(ctx: ToolContext, path: str = ".", recursive: bool = False, pattern: str = "*") -> str:
    root = ctx.resolve_path(path)
    if not root.is_dir():
        raise ToolError(f"Директорію не знайдено: {path}")
    entries: list[str] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            rel = os.path.relpath(dirpath, root)
            for name in sorted(filenames):
                if fnmatch.fnmatch(name, pattern):
                    entries.append(os.path.normpath(os.path.join(rel, name)))
            if len(entries) > 2000:
                break
    else:
        for e in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if fnmatch.fnmatch(e.name, pattern):
                entries.append(e.name + "/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} B)")
    if not entries:
        return f"{root}: порожньо"
    return truncate(f"{root}:\n" + "\n".join(entries), ctx.config.max_tool_output_chars)


def _grep(ctx: ToolContext, regex: str, path: str, glob: str) -> str:
    root = ctx.resolve_path(path)
    try:
        rx = re.compile(regex, re.IGNORECASE)
    except re.error as e:
        raise ToolError(f"Невірний regex: {e}") from e
    hits: list[str] = []
    files = [root] if root.is_file() else None
    if files is None:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            files.extend(os.path.join(dirpath, n) for n in filenames if fnmatch.fnmatch(n, glob))
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="strict") as fh:
                for no, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(f, ctx.config.workspace)}:{no}: {line.rstrip()[:300]}")
                        if len(hits) >= 300:
                            break
        except (UnicodeDecodeError, OSError):
            continue
        if len(hits) >= 300:
            break
    return "\n".join(hits) if hits else "Збігів не знайдено"


async def search_files(ctx: ToolContext, regex: str, path: str = ".", glob: str = "*") -> str:
    out = await asyncio.to_thread(_grep, ctx, regex, path, glob)
    return truncate(out, ctx.config.max_tool_output_chars)


_PATH = {"type": "string", "description": "Шлях (відносно робочої директорії)"}

TOOLS = [
    Tool(
        name="read_file",
        description="Читає текстовий файл (з номерами рядків). Для великих файлів використовуй start_line.",
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH,
                "start_line": {"type": "integer", "description": "Перший рядок (з 1)"},
                "max_lines": {"type": "integer", "description": "Скільки рядків читати"},
            },
            "required": ["path"],
        },
        func=read_file,
    ),
    Tool(
        name="write_file",
        description="Створює або перезаписує файл (або дописує при append=true). Потребує підтвердження користувача.",
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH,
                "content": {"type": "string", "description": "Повний вміст файлу"},
                "append": {"type": "boolean", "description": "Дописати в кінець замість перезапису"},
            },
            "required": ["path", "content"],
        },
        func=write_file,
        dangerous=True,
        summarize=lambda a: f"{'Дописати у' if a.get('append') else 'Записати'} файл {a.get('path')} "
        f"({len(str(a.get('content', '')))} символів):\n{str(a.get('content', ''))[:600]}",
    ),
    Tool(
        name="edit_file",
        description="Замінює унікальний фрагмент old_text на new_text у файлі. Потребує підтвердження.",
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH,
                "old_text": {"type": "string", "description": "Точний фрагмент, який треба замінити"},
                "new_text": {"type": "string", "description": "Новий текст"},
            },
            "required": ["path", "old_text", "new_text"],
        },
        func=edit_file,
        dangerous=True,
        summarize=lambda a: f"Змінити файл {a.get('path')}:\n- {str(a.get('old_text'))[:300]}\n+ {str(a.get('new_text'))[:300]}",
    ),
    Tool(
        name="list_dir",
        description="Показує вміст директорії (recursive=true — рекурсивно, pattern — фільтр на кшталт '*.py').",
        parameters={
            "type": "object",
            "properties": {
                "path": _PATH,
                "recursive": {"type": "boolean"},
                "pattern": {"type": "string", "description": "glob-фільтр імен файлів"},
            },
        },
        func=list_dir,
    ),
    Tool(
        name="search_files",
        description="Шукає regex у файлах (як grep). glob обмежує типи файлів, напр. '*.md'.",
        parameters={
            "type": "object",
            "properties": {
                "regex": {"type": "string", "description": "Регулярний вираз (без урахування регістру)"},
                "path": _PATH,
                "glob": {"type": "string"},
            },
            "required": ["regex"],
        },
        func=search_files,
    ),
]
