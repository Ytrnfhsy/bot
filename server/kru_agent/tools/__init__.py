"""Реєстр інструментів агента."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import Config
from ..storage import Storage


class ToolError(Exception):
    """Очікувана помилка інструмента — текст повертається моделі."""


@dataclass
class ToolContext:
    """Контекст однієї задачі: хто її дав, у якій теці працювати, які інструменти дозволено."""

    config: Config
    storage: Storage
    owner: str = "owner"  # чия пам'ять і чати
    workspace: Path | None = None  # None — KRU_WORKSPACE
    allow_outside: bool | None = None  # None — KRU_ALLOW_OUTSIDE_WORKSPACE
    denied_tools: frozenset[str] = frozenset()  # недоступні цьому користувачу
    vision_ok: bool = True  # чи може поточна модель бачити зображення
    pending_images: list[str] = field(default_factory=list)  # картинки, які view_image просить показати моделі
    memory: Any = None  # semantic.MemoryIndex — пошук пам'яті за змістом (None — лише за словами)
    llm: Any = None  # модель — для інструментів, яким треба «подумати» (deep_research)
    progress: Callable[[str], Awaitable[None]] | None = None  # проміжний стан довгого інструмента
    auto_files: bool = False  # запис файлів у робочій теці без запиту (налаштування користувача)
    approved_tools: set[str] = field(default_factory=set)  # «дозволяти до кінця задачі»
    plan_hook: Callable[[list[dict[str, str]]], Awaitable[None]] | None = None  # update_plan → чек-лист чату
    delegate_hook: Callable[[str, str], Awaitable[str]] | None = None  # delegate → субагент

    async def report(self, text: str) -> None:
        if self.progress is not None:
            await self.progress(text)

    def __post_init__(self) -> None:
        if self.workspace is None:
            self.workspace = self.config.workspace
        if self.allow_outside is None:
            self.allow_outside = self.config.allow_outside_workspace

    def resolve_path(self, path: str) -> Path:
        """Шлях відносно робочої директорії; заборона виходу за її межі (якщо не дозволено)."""
        ws = self.workspace.resolve()
        p = Path(path).expanduser()
        full = (p if p.is_absolute() else ws / p).resolve()
        if not self.allow_outside and full != ws and ws not in full.parents:
            raise ToolError(
                f"Шлях {path!r} поза робочою директорією {ws}. Використовуй відносні шляхи."
            )
        return full


ToolFunc = Callable[..., Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunc
    dangerous: bool = False
    # Короткий опис дії для запиту підтвердження
    summarize: Callable[[dict[str, Any]], str] | None = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }

    def describe(self, args: dict[str, Any]) -> str:
        if self.summarize:
            try:
                return self.summarize(args)
            except Exception:  # noqa: BLE001
                pass
        parts = []
        for k, v in args.items():
            text = v if isinstance(v, str) else repr(v)
            parts.append(f"{k}={text[:120]}")
        return ", ".join(parts) or "(без параметрів)"


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def schemas(self, denied: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values() if t.name not in denied]

    def text_catalog(self, denied: frozenset[str] = frozenset()) -> str:
        """Опис інструментів для JSON-режиму (коли нативні tool calls недоступні)."""
        import json

        lines = []
        for t in self.tools.values():
            if t.name in denied:
                continue
            lines.append(f"- {t.name}: {t.description}\n  parameters: {json.dumps(t.parameters, ensure_ascii=False)}")
        return "\n".join(lines)

    async def call(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        allowed = ", ".join(n for n in self.tools if n not in ctx.denied_tools)
        if tool is None:
            raise ToolError(f"Невідомий інструмент {name!r}. Доступні: {allowed}")
        if name in ctx.denied_tools:
            raise ToolError(f"Інструмент {name!r} недоступний цьому користувачу. Доступні: {allowed}")
        sig = inspect.signature(tool.func)
        accepted = {k: v for k, v in args.items() if k in sig.parameters}
        missing = [
            p.name
            for p in list(sig.parameters.values())[1:]
            if p.default is inspect.Parameter.empty and p.name not in accepted
        ]
        if missing:
            raise ToolError(f"Бракує обов'язкових параметрів: {', '.join(missing)}")
        try:
            return await tool.func(ctx, **accepted)
        except TypeError as e:
            raise ToolError(f"Невірні аргументи для {name}: {e}") from e


def build_registry() -> ToolRegistry:
    from . import files, memory, plan, python_exec, research, schedule, shell, system, telegram, vision, web

    reg = ToolRegistry()
    for module in (web, research, files, shell, python_exec, memory, plan, schedule, telegram, system, vision):
        for tool in module.TOOLS:
            reg.register(tool)
    return reg


def preview(text: str, limit: int = 20000) -> str:
    """Текст для запиту на дозвіл: користувач має бачити, що саме погоджує (у роботі — з прокруткою)."""
    text = str(text)
    return text if len(text) <= limit else f"{text[:limit]}\n… (ще {len(text) - limit} символів)"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n\n…[обрізано {len(text) - limit} символів]…\n\n{text[-tail:]}"
