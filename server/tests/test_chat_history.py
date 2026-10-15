"""search_chat / read_chat: точні деталі з давньої (вже стиснутої) частини поточного чату."""

from __future__ import annotations

import pytest

from kru_agent.agent import Agent
from kru_agent.tools import ToolError

from .conftest import FakeLLM, call, text
from .test_agent import Recorder


def _chat(agent, sid="c"):
    agent.storage.save_session(sid, [
        {"role": "user", "content": "[Context note from the system — not written by the user]\nCurrent local time: x"},
        {"role": "user", "content": "Перекладай термін «stakeholder» як «зацікавлена сторона»."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "web_search", "arguments": {"query": "курс долара"}}}]},
        {"role": "tool", "tool_name": "web_search", "content": "НБУ: курс 41,52 грн за долар"},
        {"role": "assistant", "content": "Курс — 41,52 грн. Термін «stakeholder» запам'ятав."},
        {"role": "user", "content": "TOOL RESULT (read_file):\nрядок із секретним кодом 7781"},
    ])


async def test_search_and_read(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    _chat(agent)
    ctx = agent.make_context()
    ctx.session_id = "c"
    out = await agent.registry.call(ctx, "search_chat", {"query": "stakeholder"})
    assert "#2 (користувач)" in out and "#5 (агент)" in out and "Context note" not in out
    assert "#4 (інструмент web_search): НБУ: курс 41,52" in await agent.registry.call(
        ctx, "search_chat", {"query": "курс НБУ"})  # результати інструментів теж шукаються
    assert "#6 (read_file)" in await agent.registry.call(ctx, "search_chat", {"query": "7781"})  # JSON-режим
    assert "виклик web_search" in await agent.registry.call(ctx, "search_chat", {"query": "курс долара"})
    assert "нічого не знайдено" in await agent.registry.call(ctx, "search_chat", {"query": "біткоїн"})
    full = await agent.registry.call(ctx, "read_chat", {"number": 4, "count": 2})
    assert full.startswith("#4 (інструмент web_search):\nНБУ") and "#5 (агент)" in full
    assert (await agent.registry.call(ctx, "read_chat", {"number": 1})).startswith("#2")  # примітку пропущено
    with pytest.raises(ToolError, match="Немає повідомлення #99"):
        await agent.registry.call(ctx, "read_chat", {"number": 99})
    with pytest.raises(ToolError, match="Порожній"):
        await agent.registry.call(ctx, "search_chat", {"query": "  "})
    ctx.session_id = ""
    with pytest.raises(ToolError, match="Немає поточного чату"):
        await agent.registry.call(ctx, "search_chat", {"query": "курс"})


async def test_agent_finds_detail_from_summarized_part(cfg):
    """Довга розмова стиснута в підсумок — модель знаходить точну деталь у повній історії свого чату."""
    cfg.max_history_chars = 600
    llm = FakeLLM([
        text("Домовились: «stakeholder» = «зацікавлена сторона». " + "а" * 300),
        text("- перекладаємо книгу"),  # підсумок (без терміна)
        call("search_chat", query="stakeholder"),
        text("Ми перекладали його як «зацікавлена сторона»."),
    ])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("c", "Як перекладати stakeholder?", rec.emit, rec.confirm)
    await agent.run("c", "нагадай, як ми перекладали той термін? " + "x" * 200, rec.emit, rec.confirm)
    system = llm.calls[2]["messages"][0]["content"]
    assert "перекладаємо книгу" in system and "use search_chat" in system  # підказка поруч із підсумком
    result = next(e for e in rec.events if e["type"] == "tool_result" and e["name"] == "search_chat")
    assert result["ok"] and "зацікавлена сторона" in result["preview"]
