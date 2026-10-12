"""Виконання Python-коду в окремому процесі (з підтвердженням)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from . import Tool, ToolContext
from .shell import run_process


async def run_python(ctx: ToolContext, code: str, timeout: int = 0) -> str:
    tmp_dir = ctx.workspace / ".kru_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".py", dir=tmp_dir, delete=False, encoding="utf-8") as f:
        f.write(code)
        script = Path(f.name)
    try:
        t = int(timeout) or ctx.config.python_timeout
        return await run_process(
            [sys.executable, "-u", str(script)],
            str(ctx.workspace),
            min(t, 3600),
            ctx.config.max_tool_output_chars,
        )
    finally:
        script.unlink(missing_ok=True)


TOOLS = [
    Tool(
        name="run_python",
        description=(
            "Виконує Python-скрипт (окремий процес, робоча директорія — workspace) і повертає вивід. "
            "Використовуй print() для результатів. Добре для обчислень, аналізу даних, обробки файлів. "
            "Потребує підтвердження користувача."
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Повний Python-код"},
                "timeout": {"type": "integer", "description": "Таймаут у секундах"},
            },
            "required": ["code"],
        },
        func=run_python,
        dangerous=True,
        summarize=lambda a: f"Виконати Python-код:\n{str(a.get('code', ''))[:1500]}",
    ),
]
