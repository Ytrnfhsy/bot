"""MCP-сервери як інструменти агента: запуск по stdio, виклики, картинки, помилки, перезапуск, дозволи."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kru_agent.agent import Agent
from kru_agent.mcp import MCPManager, load_specs
from kru_agent.tools import ToolError, ToolRegistry
from kru_agent.users import User

from .conftest import FakeLLM

FAKE = str(Path(__file__).with_name("fake_mcp_server.py"))


def _config(cfg, **extra):
    cfg.mcp_config = cfg.data_dir / "mcp.json"
    spec = {"command": sys.executable, "args": ["-I", FAKE, "{workspace}"], **extra}
    cfg.mcp_config.write_text(json.dumps({"mcpServers": {"fake pc": spec, "off": {"command": "x", "enabled": False}}}))
    return cfg


def test_specs(cfg):
    assert load_specs(_config(cfg)).keys() == {"fake_pc"}  # назва — без пробілів; вимкнений пропущено
    cfg.mcp_config = cfg.data_dir / "missing.json"
    assert load_specs(cfg) == {}


async def test_tools_calls_images_errors_restart(cfg):
    manager, reg = MCPManager(_config(cfg)), ToolRegistry()
    try:
        assert await manager.start(reg) == []
        assert set(reg.tools) == {"fake_pc_echo", "fake_pc_doc_preview", "fake_pc_fail", "fake_pc_crash"}  # усі сторінки
        assert manager.admin_only == set(reg.tools)
        echo = reg.get("fake_pc_echo")
        assert echo.dangerous and echo.parameters["required"] == ["text"] and "[fake_pc]" in echo.description
        agent = Agent(cfg, llm=FakeLLM([]), registry=reg)
        ctx = agent.make_context()
        out = await reg.call(ctx, "fake_pc_echo", {"text": "привіт", "extra": 1})
        assert out == "ехо: привіт ['extra', 'text']"  # аргументи передаються всі — схему перевіряє сервер
        out = await reg.call(ctx, "fake_pc_doc_preview", {})
        assert "ось прев'ю" in out and ctx.pending_images and ctx.pending_images[0].startswith("mcp/fake_pc-")
        assert (ctx.workspace / ctx.pending_images[0]).read_bytes()[:4] == b"\x89PNG"
        with pytest.raises(ToolError, match="документ не відкрито"):
            await reg.call(ctx, "fake_pc_fail", {})
        with pytest.raises(ToolError, match="завершився"):
            await reg.call(ctx, "fake_pc_crash", {})
        assert (await reg.call(ctx, "fake_pc_echo", {"text": "знову"})).startswith("ехо: знову")  # піднявся сам
    finally:
        await manager.stop()


async def test_agent_permissions(cfg):
    agent = Agent(_config(cfg, confirm=False), llm=FakeLLM([]))
    try:
        assert await agent.start_mcp() == []
        assert not agent.registry.get("fake_pc_echo").dangerous  # confirm: false — без запиту дозволу
        assert "fake_pc_echo" not in agent.make_context().denied_tools
        assert "fake_pc_echo" in agent.make_context(User("guest", "user")).denied_tools  # лише адміністратор
    finally:
        await agent.close()


async def test_bad_command(cfg):
    cfg.mcp_config = cfg.data_dir / "mcp.json"
    cfg.mcp_config.write_text(json.dumps({"mcpServers": {"nope": {"command": "/nonexistent/photocraft-cli"}}}))
    manager, reg = MCPManager(cfg), ToolRegistry()
    errors = await manager.start(reg)
    assert errors and errors[0].startswith("nope: не вдалося запустити") and reg.tools == {}
    await manager.stop()
