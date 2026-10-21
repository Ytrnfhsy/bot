"""MCP-сервери як інструменти агента (Model Context Protocol, транспорт stdio).

Налаштування — ~/.config/kru/mcp.json (KRU_MCP_CONFIG), у тому ж форматі, що й у Claude Desktop / Claude Code:

    {"mcpServers": {"photocraft": {"command": "photocraft-cli",
                                   "args": ["mcp", "--automation-read-root", "{workspace}",
                                            "--automation-write-root", "{workspace}"],
                                   "confirm": false}}}

{workspace} — робоча тека власника (KRU_WORKSPACE). Власні поля Kru:
  confirm    — питати дозволу перед кожним викликом (типово true; false — сервер і так обмежений своєю текою);
  admin_only — лише для адміністраторів (типово true);
  enabled    — false, щоб тимчасово вимкнути.
Кожен інструмент сервера стає інструментом агента <сервер>_<інструмент> (photocraft_doc_open). Картинки, які
повертає сервер, зберігаються в workspace/mcp/ і показуються моделі (якщо вона бачить зображення).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any

from . import __version__
from .tools import Tool, ToolContext, ToolError

log = logging.getLogger("kru.mcp")

PROTOCOL = "2025-06-18"
START_TIMEOUT = 30.0
CALL_TIMEOUT = 600.0
IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


class MCPError(Exception):
    pass


def load_specs(cfg) -> dict[str, dict[str, Any]]:
    path = Path(cfg.mcp_config).expanduser()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as e:
        log.error("MCP: не вдалося прочитати %s: %s", path, e)
        return {}
    servers = data.get("mcpServers") or data.get("servers") or {}
    out = {}
    for name, spec in servers.items() if isinstance(servers, dict) else ():
        if isinstance(spec, dict) and spec.get("command") and spec.get("enabled", True) is not False:
            out[re.sub(r"[^A-Za-z0-9_-]", "_", str(name))[:24]] = spec
    return out


def _expand(value: str, cfg) -> str:
    return str(value).replace("{workspace}", str(Path(cfg.workspace).expanduser().resolve())) \
        .replace("{data_dir}", str(Path(cfg.data_dir).expanduser()))


class MCPServer:
    """Один MCP-сервер у дочірньому процесі: JSON-RPC 2.0 по рядках у stdin/stdout."""

    def __init__(self, name: str, spec: dict[str, Any], cfg) -> None:
        self.name, self.spec, self.cfg = name, spec, cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.tools: list[dict[str, Any]] = []
        self.info: dict[str, Any] = {}
        self._pending: dict[int, asyncio.Future] = {}
        self._next = 0
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.last_error = ""

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        cmd = _expand(self.spec["command"], self.cfg)
        args = [_expand(a, self.cfg) for a in self.spec.get("args") or []]
        env = os.environ | {str(k): _expand(v, self.cfg) for k, v in (self.spec.get("env") or {}).items()}
        cwd = _expand(self.spec["cwd"], self.cfg) if self.spec.get("cwd") else None
        Path(self.cfg.workspace).expanduser().mkdir(parents=True, exist_ok=True)  # {workspace} має існувати
        try:
            self.proc = await asyncio.create_subprocess_exec(
                cmd, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env, cwd=cwd, limit=64 * 1024 * 1024)
        except OSError as e:
            raise MCPError(f"не вдалося запустити {cmd}: {e}") from e
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())
        self.info = await self.request("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": {"name": "kru", "version": __version__}}, timeout=START_TIMEOUT)
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools, cursor = [], None
        while True:
            res = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=START_TIMEOUT)
            tools += [t for t in res.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools
        log.info("MCP %s: %d інструментів", self.name, len(tools))

    async def stop(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 3)
            except (asyncio.TimeoutError, OSError, ProcessLookupError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
        self._fail_pending(MCPError("сервер зупинено"))

    async def _send(self, msg: dict[str, Any]) -> None:
        if not self.alive:
            raise MCPError(f"MCP-сервер {self.name} не працює")
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any], timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        self._next += 1
        rid = self._next
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            raise MCPError(f"{self.name}: {method} не відповів за {int(timeout)} с") from e
        finally:
            self._pending.pop(rid, None)

    async def _read(self) -> None:
        try:
            while line := await self.proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.debug("MCP %s: не JSON: %s", self.name, line[:200])
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg:  # запит чи сповіщення від сервера
                    if "id" in msg:
                        await self._answer(msg)
                    continue
                fut = self._pending.get(msg.get("id"))
                if fut is None or fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"] or {}
                    fut.set_exception(MCPError(f"{self.name}: {err.get('message', err)}"))
                else:
                    fut.set_result(msg.get("result") or {})
        except (OSError, ValueError) as e:
            log.warning("MCP %s: читання обірвалось: %s", self.name, e)
        finally:
            self._fail_pending(MCPError(f"MCP-сервер {self.name} завершився{': ' + self.last_error if self.last_error else ''}"))

    async def _answer(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "ping":
            result: dict[str, Any] | None = {}
        elif method == "roots/list":
            result = {"roots": [{"uri": Path(self.cfg.workspace).expanduser().resolve().as_uri(), "name": "workspace"}]}
        else:
            result = None
        reply = {"jsonrpc": "2.0", "id": msg["id"]}
        reply.update({"result": result} if result is not None else
                     {"error": {"code": -32601, "message": f"метод {method} не підтримується"}})
        try:
            await self._send(reply)
        except (MCPError, OSError):
            pass

    async def _drain_stderr(self) -> None:
        try:
            while line := await self.proc.stderr.readline():
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.last_error = text[-300:]
                    log.debug("MCP %s: %s", self.name, text)
        except (OSError, ValueError):
            pass

    def _fail_pending(self, err: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:  # процес упав — підняти знову (сесія документів почнеться з нуля)
            if not self.alive:
                log.info("MCP %s: перезапускаю", self.name)
                await self.start()
        return await self.request("tools/call", {"name": tool, "arguments": args})


def tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"{server}_{tool}")[:64]


def render_result(result: dict[str, Any], ctx: ToolContext, server: str) -> str:
    """Відповідь tools/call → текст для моделі; картинки — у workspace/mcp/ і моделі на огляд."""
    parts: list[str] = []
    for item in result.get("content") or []:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "image":
            ext = IMAGE_EXT.get(str(item.get("mimeType")), "png")
            folder = ctx.workspace / "mcp"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{server}-{uuid.uuid4().hex[:8]}.{ext}"
            try:
                path.write_bytes(base64.b64decode(str(item.get("data", "")), validate=True))
            except (ValueError, OSError) as e:
                parts.append(f"[зображення не збережено: {e}]")
                continue
            rel = path.relative_to(ctx.workspace).as_posix()
            if ctx.vision_ok:
                ctx.pending_images.append(rel)
                parts.append(f"[зображення {rel} — додано до розмови, роздивись його]")
            else:
                parts.append(f"[зображення збережено: {rel}]")
        elif kind == "resource":
            res = item.get("resource") or {}
            parts.append(str(res.get("text")) if res.get("text") is not None else f"[ресурс {res.get('uri', '')}]")
        elif kind == "resource_link":
            parts.append(f"[ресурс {item.get('uri', '')}: {item.get('name', '')}]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    text = "\n".join(p for p in parts if p).strip() or "(порожня відповідь)"
    if result.get("isError"):
        raise ToolError(text)
    return text


class MCPManager:
    """Усі MCP-сервери з налаштувань: запуск, інструменти для агента, зупинка."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.servers = {name: MCPServer(name, spec, cfg) for name, spec in load_specs(cfg).items()}
        self.admin_only: set[str] = set()  # імена інструментів лише для адміністраторів

    async def start(self, registry) -> list[str]:
        """Запустити сервери й додати їхні інструменти в реєстр агента. → помилки (по рядку на сервер)."""
        errors = []
        for name, server in self.servers.items():
            try:
                await server.start()
            except (MCPError, OSError) as e:
                errors.append(f"{name}: {e}")
                log.error("MCP %s: %s", name, e)
                continue
            for spec in server.tools:
                tool = self._tool(server, spec)
                registry.register(tool)
                if server.spec.get("admin_only", True) is not False:
                    self.admin_only.add(tool.name)
        return errors

    async def stop(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.servers.values()), return_exceptions=True)

    def _tool(self, server: MCPServer, spec: dict[str, Any]) -> Tool:
        remote = str(spec["name"])
        schema = spec.get("inputSchema") if isinstance(spec.get("inputSchema"), dict) else {}
        params = {"type": "object", "properties": schema.get("properties") or {}}
        if schema.get("required"):
            params["required"] = schema["required"]
        desc = str(spec.get("description") or spec.get("title") or remote).strip()

        async def run(ctx: ToolContext, **kwargs: Any) -> str:
            try:
                result = await server.call(remote, kwargs)
            except MCPError as e:
                raise ToolError(str(e)) from e
            return render_result(result, ctx, server.name)

        def summarize(args: dict[str, Any]) -> str:
            shown = json.dumps(args, ensure_ascii=False)
            return f"{server.name}: {remote} {shown[:300]}"

        return Tool(name=tool_name(server.name, remote), description=f"[{server.name}] {desc}"[:1024],
                    parameters=params, func=run, dangerous=server.spec.get("confirm", True) is not False,
                    summarize=summarize)
