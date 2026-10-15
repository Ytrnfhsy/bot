"""Пісочниця для run_shell / run_python: одноразовий Docker-контейнер, що бачить лише робочу теку користувача.

Без root-прав і можливостей ядра, з обмеженням пам'яті/процесора/кількості процесів, файлова система
контейнера — лише для читання (крім /workspace і /tmp). Тому команди в ній можна виконувати без запиту
дозволу. Образ: deploy/sandbox.sh build (kru-sandbox — Python і типові бібліотеки/утиліти).

Бібліотеки, які агент ставить через pip, лягають у <тека>/.kru-py і лишаються між запусками.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid

from .tools import ToolContext, ToolError

WORKDIR = "/workspace"
PY_USER = f"{WORKDIR}/.kru-py"


def enabled(config) -> bool:
    return str(getattr(config, "sandbox", "off")).lower() == "docker"


def docker_command(ctx: ToolContext, inner: list[str], name: str) -> list[str]:
    cfg = ctx.config
    ws = str(ctx.workspace.resolve())
    user = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "1000:1000"
    return [
        "docker", "run", "--rm", "--name", name,
        "--pull", "never",  # лише локально зібраний образ — ніколи не тягнути однойменний чужий із Docker Hub
        "--user", user,  # файли в теці — від імені власника, не root
        "-v", f"{ws}:{WORKDIR}", "-w", WORKDIR,
        "--read-only", "--tmpfs", "/tmp:rw,exec,size=512m",
        "--memory", cfg.sandbox_memory, "--cpus", str(cfg.sandbox_cpus), "--pids-limit", "256",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--network", cfg.sandbox_network,
        "-e", "HOME=/tmp", "-e", f"PYTHONUSERBASE={PY_USER}", "-e", "PIP_USER=1",
        "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "MPLCONFIGDIR=/tmp",
        "-e", f"PATH={PY_USER}/bin:/usr/local/bin:/usr/bin:/bin",
        cfg.sandbox_image, *inner,
    ]


async def run(ctx: ToolContext, inner: list[str], timeout: int) -> str:
    """Виконати команду в контейнері; після тайм-ауту чи скасування контейнер знищується."""
    from .tools.shell import run_process

    if shutil.which("docker") is None:
        raise ToolError("Пісочниця недоступна: на сервері немає docker. Встанови Docker або вимкни KRU_SANDBOX.")
    name = f"kru-sbx-{uuid.uuid4().hex[:10]}"
    # тайм-аут — усередині контейнера (точний); зовнішній — лише страховка, якщо Docker завис
    inner = ["timeout", "-k", "5", str(timeout), *inner]
    try:
        out = await run_process(docker_command(ctx, inner, name), str(ctx.workspace), timeout + 30,
                                ctx.config.max_tool_output_chars)
    finally:
        # docker run --rm прибирає сам, але після тайм-ауту/скасування контейнер ще живий
        await _remove(name)
    if ("Unable to find image" in out or "No such image" in out) and ctx.config.sandbox_image in out:
        raise ToolError(f"Немає образу пісочниці {ctx.config.sandbox_image}. На сервері: deploy/sandbox.sh build")
    if "permission denied while trying to connect to the docker" in out.lower():
        raise ToolError("Агент не має доступу до Docker (додай користувача в групу docker і перезапусти сервіс)")
    if out.startswith(("[код виходу 124]", "[код виходу 137]")):
        out = f"[ТАЙМАУТ після {timeout} с — процес зупинено]" + out.split("]", 1)[1]
    return out


async def _remove(name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=20)
    except Exception:  # noqa: BLE001
        pass
