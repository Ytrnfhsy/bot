"""«↻ Повторити»: остання задача чату виконується заново, попередня спроба прибирається з розмови."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kru_agent.agent import Agent
from kru_agent.server import create_app

from .conftest import FakeLLM, call, text
from .test_agent import Recorder
from .test_server import _until
from .test_telegram_bot import make, msg


async def test_retry_replaces_last_attempt(cfg):
    llm = FakeLLM([text("Канберра"), call("current_datetime"), text("Погана відповідь"), text("Краща відповідь")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "Столиця Австралії?", rec.emit, rec.confirm)
    await agent.run("s", "А який там час?", rec.emit, rec.confirm)
    answer = await agent.run("s", "", rec.emit, rec.confirm, retry=True)
    assert answer == "Краща відповідь"
    # модель отримала ту саму задачу, а попередньої спроби (виклик інструмента й відповідь) у контексті немає
    sent = llm.calls[-1]["messages"]
    assert sent[-1]["role"] == "user" and sent[-1]["content"].startswith("А який там час?")
    assert not any("Погана" in str(m.get("content")) for m in sent)
    roles = [(m["role"], m.get("content")) for m in agent.storage.load_session("s")]
    assert roles == [("user", "Столиця Австралії?"), ("assistant", "Канберра"),
                     ("user", "А який там час?"), ("assistant", "Краща відповідь")]
    assert agent.list_chats()[0]["count"] == 2


async def test_retry_after_cancel_resets_plan(cfg):
    agent = Agent(cfg, llm=FakeLLM([text("ок")]))
    agent.storage.save_session("s", [{"role": "user", "content": "Переклади книгу"},
                                     {"role": "assistant", "content": "", "tool_calls": [
                                         {"function": {"name": "update_plan", "arguments": {}}}]},
                                     {"role": "tool", "tool_name": "update_plan", "content": "ok"}])
    agent.storage.save_plan("s", [{"title": "Розділ 1", "status": "in_progress"}])
    rec = Recorder()
    await agent.run("s", "", rec.emit, rec.confirm, retry=True)
    assert {"type": "plan", "steps": []} in rec.events
    assert [m["role"] for m in agent.storage.load_session("s")] == ["user", "assistant"]


async def test_retry_empty_chat(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    rec = Recorder()
    with pytest.raises(ValueError, match="немає задачі"):
        await agent.run("new", "", rec.emit, rec.confirm, retry=True)


def test_ws_retry(cfg):
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM([text("1"), text("2")]))
    client = TestClient(create_app(cfg, agent=agent))
    with client, client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "task", "text": "скажи число", "session_id": "t"})
        assert _until(ws, "final")[0]["text"] == "1"
        ws.send_json({"type": "task", "text": "", "retry": True, "session_id": "t"})
        assert _until(ws, "final")[0]["text"] == "2"
        ws.send_json({"type": "task", "retry": True, "session_id": "порожній"})
        assert "немає задачі" in _until(ws, "error")[0]["message"]
    assert [m["content"] for m in agent.storage.load_session("t")] == ["скажи число", "2"]


async def test_telegram_retry(cfg):
    gw, agent, bot = make(cfg, [text("перша"), text("друга")])
    await gw.handle(msg("/retry"))
    assert "немає задачі" in bot.texts()[-1]
    await gw.handle(msg("порахуй щось"))
    await gw.handle(msg("/retry"))
    assert bot.texts()[-1] == "друга"
    sid = agent.storage.telegram_session("owner")
    assert [m["content"] for m in agent.storage.load_session(sid)] == ["порахуй щось", "друга"]
