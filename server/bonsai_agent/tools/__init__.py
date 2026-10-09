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
    config: Config
    storage: Storage

    def resolve_path(self, path: str) -> Path:
        """Шлях відносно робочої директорії; заборона виходу за її межі (якщо не дозволено)."""
        ws = self.config.workspace.resolve()
        p = Path(path).expanduser()
        full = (p if p.is_absolute() else ws / p).resolve()
        if not self.config.allow_outside_workspace and full != ws and ws not in full.parents:
            raise ToolError(
                f"Шлях {path!r} поза робочою директорією {ws}. "
                "Використовуй відносні шляхи (або увімкни BONSAI_ALLOW_OUTSIDE_WORKSPACE)."
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
        return f"{self.name}({args})"


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def text_catalog(self) -> str:
        """Опис інструментів для JSON-режиму (коли нативні tool calls недоступні)."""
        import json

        lines = []
        for t in self.tools.values():
            lines.append(f"- {t.name}: {t.description}\n  parameters: {json.dumps(t.parameters, ensure_ascii=False)}")
        return "\n".join(lines)

    async def call(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(f"Невідомий інструмент {name!r}. Доступні: {', '.join(self.tools)}")
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
    from . import files, memory, python_exec, shell, system, web

    reg = ToolRegistry()
    for module in (web, files, shell, python_exec, memory, system):
        for tool in module.TOOLS:
            reg.register(tool)
    return reg


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return f"{text[:head]}\n\n…[обрізано {len(text) - limit} символів]…\n\n{text[-tail:]}"
