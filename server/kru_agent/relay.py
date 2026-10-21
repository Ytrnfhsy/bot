"""MCP-сервери на ПК користувача, які пробрасує його робот (PhotoCraft у вікні, яке видно наживо).

Робот сам запускає MCP-сервери з ~/.config/kru-robot/mcp.json і через своє WebSocket-з'єднання повідомляє
їхні інструменти:
  робот → {"type": "mcp_tools", "servers": {"photocraft": {"tools": [...], "confirm": false}}, "exchange": true}
  сервер → {"type": "mcp_call", "id", "server", "tool", "args"}   робот → {"type": "mcp_result", "id", "result"|"error"}
  сервер → {"type": "pc_put", "id", "name", "data"} / {"type": "pc_get", "id", "name"}  — обмін файлами з текою на ПК
Інструменти отримують назви pc_<сервер>_<інструмент> і доступні лише власникові робота, поки робот підключений.
Без SSH, відкритих портів і ключів: усе йде тим самим з'єднанням, що й чат.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools import Tool, ToolContext, ToolError

log = logging.getLogger("kru.relay")

CALL_TIMEOUT = 600.0
MAX_FILE = 25 * 1024 * 1024
FILE_TOOLS = ("pc_put_file", "pc_get_file")


@dataclass
class Link:
    conn: Any  # server.Connection робота (send())
    tools: dict[str, tuple[str, str]] = field(default_factory=dict)  # назва в Kru → (сервер, інструмент)
    exchange: bool = False
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


def relay_name(server: str, tool: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"pc_{server}_{tool}")[:64]


class RobotRelay:
    def __init__(self, registry) -> None:
        self.registry = registry
        self.links: dict[str, Link] = {}  # власник → його робот (останній, що надіслав mcp_tools)
        self.names: set[str] = set()  # усі інструменти, які колись реєструвало реле

    # ---------------- з'єднання робота ----------------
    def attach(self, owner: str, conn, msg: dict[str, Any]) -> list[str]:
        """Робот повідомив свої MCP-сервери. → назви інструментів, доступних власникові."""
        old = self.links.get(owner)
        link = Link(conn, exchange=msg.get("exchange") is True)
        if old is not None and old.conn is conn:
            link.pending = old.pending  # те саме з'єднання оновило список — виклики в дорозі не губимо
        servers = msg.get("servers") if isinstance(msg.get("servers"), dict) else {}
        for server, info in servers.items():
            if not isinstance(info, dict):
                continue
            server = re.sub(r"[^A-Za-z0-9_-]", "_", str(server))[:24]
            dangerous = info.get("confirm", True) is not False
            for spec in info.get("tools") or []:
                if isinstance(spec, dict) and spec.get("name"):
                    name = relay_name(server, str(spec["name"]))
                    link.tools[name] = (server, str(spec["name"]))
                    self.registry.register(self._tool(name, server, spec, dangerous))
        if link.exchange:
            for tool in self._file_tools():
                link.tools[tool.name] = ("", tool.name)
                self.registry.register(tool)
        self.names |= set(link.tools)
        self.links[owner] = link
        log.info("Робот %s: %d інструментів з ПК", owner, len(link.tools))
        return sorted(link.tools)

    def detach(self, conn) -> None:
        for owner, link in list(self.links.items()):
            if link.conn is conn:
                del self.links[owner]
                for fut in link.pending.values():
                    if not fut.done():
                        fut.set_exception(ToolError("робот відключився від сервера — ПК зараз недоступний"))

    def resolve(self, conn, msg: dict[str, Any]) -> None:
        for link in self.links.values():
            if link.conn is conn and (fut := link.pending.get(str(msg.get("id")))) and not fut.done():
                if msg.get("error"):
                    fut.set_exception(ToolError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result") if isinstance(msg.get("result"), dict) else {})

    def unavailable(self, owner: str) -> frozenset[str]:
        """Інструменти реле, яких у цього користувача зараз немає (інший робот, робот вимкнений)."""
        have = self.links[owner].tools if owner in self.links else {}
        return frozenset(n for n in self.names if n not in have)

    # ---------------- виклики ----------------
    async def request(self, owner: str, msg: dict[str, Any]) -> dict[str, Any]:
        link = self.links.get(owner)
        if link is None:
            raise ToolError("робот на ПК не підключений — увімкни його й спробуй ще раз")
        rid = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        link.pending[rid] = fut
        try:
            await link.conn.send({**msg, "id": rid})
            if link.conn.closed:
                raise ToolError("робот відключився від сервера — ПК зараз недоступний")
            return await asyncio.wait_for(fut, CALL_TIMEOUT)
        except asyncio.TimeoutError as e:
            raise ToolError(f"ПК не відповів за {int(CALL_TIMEOUT)} с") from e
        finally:
            link.pending.pop(rid, None)

    def _tool(self, name: str, server: str, spec: dict[str, Any], dangerous: bool) -> Tool:
        from .mcp import render_result

        remote = str(spec["name"])
        schema = spec.get("inputSchema") if isinstance(spec.get("inputSchema"), dict) else {}
        params = {"type": "object", "properties": schema.get("properties") or {}}
        if schema.get("required"):
            params["required"] = schema["required"]
        desc = str(spec.get("description") or spec.get("title") or remote).strip()

        async def run(ctx: ToolContext, **kwargs: Any) -> str:
            result = await self.request(ctx.owner, {"type": "mcp_call", "server": server, "tool": remote,
                                                    "args": kwargs})
            return render_result(result, ctx, f"pc-{server}")

        return Tool(name=name, description=f"[ПК користувача · {server}] {desc}"[:1024], parameters=params,
                    func=run, dangerous=dangerous,
                    summarize=lambda args: f"ПК · {server}: {remote} {str(args)[:300]}")

    def _file_tools(self) -> list[Tool]:
        async def put(ctx: ToolContext, path: str, name: str = "") -> str:
            src = ctx.resolve_path(path)
            if not src.is_file():
                raise ToolError(f"Файлу {path} немає в робочій теці")
            if src.stat().st_size > MAX_FILE:
                raise ToolError("Файл завеликий (понад 25 МБ)")
            target = Path(name or src.name).name
            res = await self.request(ctx.owner, {"type": "pc_put", "name": target,
                                                 "data": base64.b64encode(src.read_bytes()).decode("ascii")})
            return (f"Скопійовано на ПК: {res.get('path', target)}. У програмах на ПК, що працюють із текою обміну "
                    f"(PhotoCraft), відкривай його як «{res.get('name', target)}».")

        async def get(ctx: ToolContext, name: str, save_as: str = "") -> str:
            res = await self.request(ctx.owner, {"type": "pc_get", "name": name})
            try:
                data = base64.b64decode(str(res.get("data", "")), validate=True)
            except ValueError as e:
                raise ToolError(f"ПК надіслав пошкоджений файл: {e}") from e
            dest = ctx.resolve_path(save_as or f"from-pc/{Path(name).name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            rel = dest.relative_to(ctx.workspace.resolve()).as_posix() if ctx.workspace.resolve() in dest.parents \
                else str(dest)
            return f"Файл з ПК збережено в робочій теці: {rel} ({len(data) // 1024} КБ)."

        return [
            Tool(name="pc_put_file",
                 description=("Скопіювати файл із робочої теки (напр. uploads/фото.jpg, надіслане користувачем) у теку "
                              "обміну на ПК користувача — її бачать програми на ПК (PhotoCraft). Повертає ім'я, "
                              "яким файл відкривати там."),
                 parameters={"type": "object", "properties": {
                     "path": {"type": "string", "description": "файл у робочій теці"},
                     "name": {"type": "string", "description": "ім'я на ПК (типово — те саме)"}},
                     "required": ["path"]},
                 func=put, summarize=lambda a: f"на ПК: {a.get('path')}"),
            Tool(name="pc_get_file",
                 description=("Забрати файл із теки обміну на ПК користувача (напр. результат, збережений PhotoCraft) "
                              "у робочу теку — щоб надіслати його користувачеві (send_file) чи обробити далі."),
                 parameters={"type": "object", "properties": {
                     "name": {"type": "string", "description": "ім'я файлу в теці обміну на ПК"},
                     "save_as": {"type": "string", "description": "шлях у робочій теці (типово from-pc/<ім'я>)"}},
                     "required": ["name"]},
                 func=get, summarize=lambda a: f"з ПК: {a.get('name')}"),
        ]
