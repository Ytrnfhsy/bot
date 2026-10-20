"""Команди й файли на віддалених машинах через SSH (лише машини зі списку `kru ssh`)."""

from __future__ import annotations

from .. import ssh
from . import Tool, ToolContext, ToolError
from .shell import run_process

MAX_TIMEOUT = 3600


def _host(ctx: ToolContext, name: str) -> ssh.Host:
    hosts = ssh.load_hosts(ctx.config)
    if not hosts:
        raise ToolError("Жодної SSH-машини не підключено. Власник додає їх на сервері: kru ssh add <назва> user@адреса")
    host = hosts.get(str(name).strip())
    if host is None:
        raise ToolError(f"Невідома машина {name!r}. Доступні: {', '.join(hosts)}")
    if not ssh.key_path(ctx.config).is_file():
        raise ToolError("Немає ключа агента. На сервері: kru ssh add …")
    return host


def _explain(out: str) -> str:
    """Код 255 — це помилка самого ssh (а не команди): підказати, що перевірити."""
    if out.startswith("[ТАЙМАУТ"):
        out += ("\n[SSH: з'єднання розірвано, але на машині команда могла лишитися працювати — перевір (ps, "
                "pgrep); довгі задачі запускай через nohup … & або systemd-run]")
    if out.startswith("[код виходу 255]"):
        hint = ""
        low = out.lower()
        if "permission denied" in low:
            hint = "ключ агента не додано на машину (kru ssh add … покаже, як)"
        elif "host key verification failed" in low or "remote host identification has changed" in low:
            hint = "змінився ключ машини (перевстановили ВМ?): kru ssh forget <назва>"
        elif "timed out" in low or "no route" in low or "could not resolve" in low or "refused" in low:
            hint = "машина недоступна — вимкнена, немає мережі (NetBird) чи не працює sshd"
        if hint:
            out += f"\n[SSH: {hint}]"
    return out


async def ssh_run(ctx: ToolContext, host: str, command: str, timeout: int = 300) -> str:
    if not command.strip():
        raise ToolError("Порожня команда")
    h = _host(ctx, host)
    t = max(5, min(int(timeout or 300), MAX_TIMEOUT))
    await ctx.report(f"{h.name}: {command[:80]}")
    out = await run_process(ssh.ssh_command(ctx.config, h, command), str(ctx.workspace), t,
                            ctx.config.max_tool_output_chars)
    return _explain(out)


async def _scp(ctx: ToolContext, h: ssh.Host, src: str, dst: str, recursive: bool) -> str:
    cmd = ssh.scp_command(ctx.config, h, src, dst, recursive)
    out = await run_process(cmd, str(ctx.workspace), 1800, ctx.config.max_tool_output_chars)
    if "subsystem request failed" in out or "sftp-server" in out.lower():
        # на машині не налаштовано SFTP (новий scp працює через нього) — старий протокол scp
        out = await run_process([cmd[0], "-O", *cmd[1:]], str(ctx.workspace), 1800, ctx.config.max_tool_output_chars)
    return out


async def ssh_upload(ctx: ToolContext, host: str, path: str, remote_path: str) -> str:
    h = _host(ctx, host)
    local = ctx.resolve_path(path)
    if not local.exists():
        raise ToolError(f"Файл не знайдено: {path}")
    out = await _scp(ctx, h, str(local), f"{h.target}:{remote_path}", local.is_dir())
    if not out.startswith("[код виходу 0]"):
        return _explain(out)
    return f"Передано {path} → {h.name}:{remote_path}"


async def ssh_download(ctx: ToolContext, host: str, remote_path: str, path: str = "") -> str:
    h = _host(ctx, host)
    name = remote_path.rstrip("/").rsplit("/", 1)[-1] or "download"
    local = ctx.resolve_path(path or f"from-{h.name}/{name}")
    local.parent.mkdir(parents=True, exist_ok=True)
    out = await _scp(ctx, h, f"{h.target}:{remote_path}", str(local), True)
    if not out.startswith("[код виходу 0]"):
        return _explain(out)
    shown = local.relative_to(ctx.workspace.resolve()) if ctx.workspace.resolve() in local.parents else local
    return f"Завантажено {h.name}:{remote_path} → {shown}"


HOST_PARAM = {"type": "string", "description": "Назва машини зі списку (наприклад, test-vm)"}

TOOLS = [
    Tool(
        name="ssh_run",
        description=(
            "Виконує команду на віддаленій машині через SSH (лише машини, які підключив користувач — їх список у "
            "системних інструкціях) і повертає stdout+stderr з кодом виходу. Без інтерактиву: sudo -n, apt-get -y."
        ),
        parameters={
            "type": "object",
            "properties": {
                "host": HOST_PARAM,
                "command": {"type": "string", "description": "Команда для shell віддаленої машини"},
                "timeout": {"type": "integer", "description": "Таймаут у секундах (типово 300, до 3600)"},
            },
            "required": ["host", "command"],
        },
        func=ssh_run,
        dangerous=True,
        summarize=lambda a: f"На машині {a.get('host')}:\n$ {a.get('command')}",
    ),
    Tool(
        name="ssh_upload",
        description="Копіює файл або теку з робочої теки на віддалену машину (scp).",
        parameters={
            "type": "object",
            "properties": {
                "host": HOST_PARAM,
                "path": {"type": "string", "description": "Файл/тека в робочій теці"},
                "remote_path": {"type": "string", "description": "Куди на машині (напр. /home/kru/app/ чи /tmp/x.sh)"},
            },
            "required": ["host", "path", "remote_path"],
        },
        func=ssh_upload,
        dangerous=True,
        summarize=lambda a: f"Передати {a.get('path')} → {a.get('host')}:{a.get('remote_path')}",
    ),
    Tool(
        name="ssh_download",
        description=(
            "Копіює файл або теку з віддаленої машини в робочу теку (типово from-<машина>/<ім'я>) — щоб прочитати, "
            "проаналізувати чи передати користувачу через send_file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "host": HOST_PARAM,
                "remote_path": {"type": "string", "description": "Шлях на машині"},
                "path": {"type": "string", "description": "Куди в робочій теці (необов'язково)"},
            },
            "required": ["host", "remote_path"],
        },
        func=ssh_download,
        dangerous=True,
        summarize=lambda a: f"Завантажити {a.get('host')}:{a.get('remote_path')}",
    ),
]

SSH_TOOLS = frozenset(t.name for t in TOOLS)
