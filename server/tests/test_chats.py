from __future__ import annotations

import pytest

from bonsai_agent.agent import Agent
from bonsai_agent.chats import default_title, to_markdown, transcript

from .conftest import FakeLLM, call, text
from .test_agent import Recorder
from .test_server import _client, _until


async def _chat(agent, session="s"):
    rec = Recorder()
    await agent.run(session, "Котра година в Києві?", rec.emit, rec.confirm)


async def test_save_list_open_export(cfg):
    agent = Agent(cfg, llm=FakeLLM([call("current_datetime"), text("Зараз **12:00**.")]))
    await _chat(agent)
    saved = agent.save_chat("s")
    assert saved["title"] == "Котра година в Києві?"
    # повторне збереження з id — оновлення, а не дубль
    agent.save_chat("s", "Час у Києві", saved["id"])
    chats = agent.list_chats()
    assert [(c["title"], c["count"]) for c in chats] == [("Час у Києві", 1)]

    agent.reset_session("s")
    assert agent.storage.load_session("s") == []
    opened = agent.open_chat(saved["id"], "s")
    assert [t["role"] for t in opened["transcript"]] == ["user", "tool", "assistant"]
    assert agent.storage.load_session("s")  # розмову відновлено — її можна продовжувати

    md = agent.export_chat(chat_id=saved["id"])["markdown"]
    assert md.startswith("# Час у Києві") and "## 🧑 Ти" in md and "## 🤖 Kru" in md and "Зараз **12:00**." in md
    assert "> 🔧 current_datetime" in md

    assert agent.delete_chat(saved["id"]) and agent.list_chats() == []


def test_empty_chat_cannot_be_saved(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    with pytest.raises(ValueError, match="порожня"):
        agent.save_chat("nothing")


def test_transcript_hides_service_messages():
    msgs = [
        {"role": "user", "content": "опиши x.png", "images": ["x.png"]},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "view_image", "arguments": {"path": "x.png"}}}]},
        {"role": "tool", "content": "додано"},
        {"role": "user", "content": "[view_image] x.png", "images": ["x.png"]},
        {"role": "user", "content": "TOOL RESULT (recall):\nнічого"},
        {"role": "assistant", "content": "<think>хм</think>Це кіт"},
    ]
    t = transcript(msgs)
    assert [x["role"] for x in t] == ["user", "tool", "assistant"]
    assert "📎 x.png" in t[0]["text"] and t[2]["text"] == "Це кіт"
    assert default_title([{"role": "user", "content": "а" * 100}]).endswith("…")
    assert "# Т" in to_markdown("Т", msgs)


def test_ws_chat_flow(cfg):
    client, agent = _client(cfg, [text("Привіт! Я Kru.")])
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "chat_save", "session_id": "r"})
        assert _until(ws, "chat_error")[0]["message"].startswith("Розмова порожня")
        ws.send_json({"type": "task", "text": "привіт", "session_id": "r"})
        _until(ws, "final")
        _until(ws, "status")
        ws.send_json({"type": "chat_save", "session_id": "r", "title": "Знайомство"})
        saved, _ = _until(ws, "chat_saved")
        lst, _ = _until(ws, "chat_list")
        assert lst["chats"][0]["title"] == "Знайомство"
        ws.send_json({"type": "reset", "session_id": "r"})
        ws.send_json({"type": "chat_open", "id": saved["id"], "session_id": "r"})
        opened, _ = _until(ws, "chat_opened")
        assert opened["transcript"][-1] == {"role": "assistant", "text": "Привіт! Я Kru."}
        ws.send_json({"type": "chat_export", "session_id": "r"})
        exp, _ = _until(ws, "chat_export")
        assert "Привіт! Я Kru." in exp["markdown"]
        ws.send_json({"type": "chat_delete", "id": saved["id"]})
        assert _until(ws, "chat_list")[0]["chats"] == []
