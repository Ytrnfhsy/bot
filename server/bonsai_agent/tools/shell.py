"""Виконання shell-команд (з підтвердженням)."""

from __future__ import annotations

import asyncio
import os
import signal

from . import Tool, ToolContext, ToolError, truncate


async def run_process(cmd: list[str] | str, cwd: str, timeout: int, limit: int, shell: bool = False) -> str:
    if shell:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
        )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill(proc)
        out = b""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        text = out.decode("utf-8", "replace")
        return truncate(f"[ТАЙМАУТ після {timeout} с — процес зупинено]\n{text}", limit)
    except asyncio.CancelledError:
        _kill(proc)
        raise
    text = out.decode("utf-8", "replace").strip() or "(порожній вивід)"
    return truncate(f"[код виходу {proc.returncode}]\n{text}", limit)


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def run_shell(ctx: ToolContext, command: str, timeout: int = 0) -> str:
    if not command.strip():
        raise ToolError("Порожня команда")
    t = int(timeout) or ctx.config.shell_timeout
    return await run_process(
        command, str(ctx.workspace), min(t, 3600), ctx.config.max_tool_output_chars, shell=True
    )


TOOLS = [
    Tool(
        name="run_shell",
        description=(
            "Виконує команду bash у робочій директорії сервера і повертає stdout+stderr. "
            "Не використовуй інтерактивні команди. Потребує підтвердження користувача."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Команда bash"},
                "timeout": {"type": "integer", "description": "Таймаут у секундах"},
            },
            "required": ["command"],
        },
        func=run_shell,
        dangerous=True,
        summarize=lambda a: f"Виконати команду:\n$ {a.get('command')}",
    ),
]
