"""MCP-сервери на ПК, які пробрасує робот: інструменти лише власника, виклики й файли тим самим з'єднанням."""

from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from kru_agent.agent import Agent
from kru_agent.llm import ChatResult
from kru_agent.server import create_app
from kru_agent.users import User

from .conftest import FakeLLM, call, text
from .test_server import _until

PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")).decode()
TOOLS = {"photocraft": {"confirm": False, "tools": [
    {"name": "doc_open", "description": "Відкрити документ", "inputSchema": {
        "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "doc_render_preview", "description": "Прев'ю", "inputSchema": {"type": "object"}}]}}


def test_relay_roundtrip(cfg):
    cfg.token = "secret"
    llm = FakeLLM([call("pc_put_file", path="uploads/cat.jpg"),
                   call("pc_photocraft_doc_open", path="cat.jpg"),
                   call("pc_photocraft_doc_render_preview"),
                   ChatResult(tool_calls=[{"function": {"name": "pc_get_file", "arguments": {"name": "cat-blur.png"}}}]),
                   text("Готово ✅")])
    agent = Agent(cfg, llm=llm)
    (cfg.workspace / "uploads").mkdir(parents=True)
    (cfg.workspace / "uploads" / "cat.jpg").write_bytes(b"JPEGDATA")
    client = TestClient(create_app(cfg, agent=agent))
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        assert agent.make_context().denied_tools >= set()  # реле ще порожнє
        ws.send_json({"type": "mcp_tools", "servers": TOOLS, "exchange": True})
        assert _until(ws, "mcp_ready")[0]["tools"] == 4
        ctx = agent.make_context()
        assert {"pc_photocraft_doc_open", "pc_put_file"}.isdisjoint(ctx.denied_tools)
        assert not agent.registry.get("pc_photocraft_doc_open").dangerous  # confirm: false з ПК
        assert "pc_photocraft_doc_open" in agent.make_context(User("guest", "user")).denied_tools  # чужий робот
        ws.send_json({"type": "task", "text": "розмий фото", "session_id": "r"})
        put, _ = _until(ws, "pc_put")
        assert put["name"] == "cat.jpg" and base64.b64decode(put["data"]) == b"JPEGDATA"
        ws.send_json({"type": "mcp_result", "id": put["id"], "result": {"path": "/home/u/Pictures/kru/cat.jpg",
                                                                          "name": "cat.jpg"}})
        mc, _ = _until(ws, "mcp_call")
        assert (mc["server"], mc["tool"], mc["args"]) == ("photocraft", "doc_open", {"path": "cat.jpg"})
        ws.send_json({"type": "mcp_result", "id": mc["id"], "error": "файл не знайдено"})  # помилка — моделі
        mc, _ = _until(ws, "mcp_call")
        ws.send_json({"type": "mcp_result", "id": mc["id"], "result": {"content": [
            {"type": "text", "text": "прев'ю"}, {"type": "image", "mimeType": "image/png", "data": PNG}]}})
        get, _ = _until(ws, "pc_get")
        ws.send_json({"type": "mcp_result", "id": get["id"], "result": {"data": base64.b64encode(b"PNGOUT").decode()}})
        final, _ = _until(ws, "final")
        assert final["text"] == "Готово ✅"
    tool_out = [m["content"] for m in llm.calls[-1]["messages"] if m.get("role") == "tool"]
    assert "cat.jpg" in tool_out[0] and "ПОМИЛКА: файл не знайдено" in tool_out[1] and "прев'ю" in tool_out[2]
    assert (cfg.workspace / "from-pc" / "cat-blur.png").read_bytes() == b"PNGOUT"
    assert list((cfg.workspace / "mcp").glob("pc-photocraft-*.png"))
    assert "pc_photocraft_doc_open" in agent.make_context().denied_tools  # робот відключився — інструментів немає


def test_relay_disconnect_fails_pending(cfg):
    import asyncio

    from kru_agent.relay import RobotRelay
    from kru_agent.tools import ToolError, ToolRegistry

    class Conn:
        closed = False

        def __init__(self):
            self.sent = []

        async def send(self, msg):
            self.sent.append(msg)

    async def scenario():
        relay, conn = RobotRelay(ToolRegistry()), Conn()
        relay.attach("owner", conn, {"servers": TOOLS})
        assert relay.unavailable("owner") == frozenset() and relay.unavailable("other") >= {"pc_photocraft_doc_open"}
        task = asyncio.create_task(relay.request("owner", {"type": "mcp_call"}))
        await asyncio.sleep(0)
        relay.detach(conn)
        try:
            await task
        except ToolError as e:
            return str(e)

    assert "відключився" in asyncio.run(scenario())
