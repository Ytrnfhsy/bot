from __future__ import annotations

import pytest

from kru_agent.agent import Agent
from kru_agent.chats import default_title, to_markdown, transcript

from .conftest import FakeLLM, call, text, real
from .test_agent import Recorder
from .test_server import _client, _until


async def _chat(agent, session="s"):
    rec = Recorder()
    await agent.run(session, "Котра година в Києві?", rec.emit, rec.confirm)


async def test_chats_are_saved_automatically(cfg):
    agent = Agent(cfg, llm=FakeLLM([call("current_datetime"), text("Зараз **12:00**.")]))
    await _chat(agent)
    chats = agent.list_chats()
    assert [(c["id"], c["title"], c["count"]) for c in chats] == [("s", "Котра година в Києві?", 1)]

    renamed = agent.rename_chat("s", "  Час   у Києві ")
    assert renamed == {"id": "s", "title": "Час у Києві"}
    opened = agent.open_chat("s")
    assert opened["title"] == "Час у Києві"
    assert [t["role"] for t in opened["transcript"]] == ["user", "tool", "assistant"]

    md = agent.export_chat("s")["markdown"]
    assert md.startswith("# Час у Києві") and "## 🧑 Ти" in md and "## 🤖 Kru" in md and "Зараз **12:00**." in md
    assert "> 🔧 current_datetime" in md

    assert agent.delete_chat("s") and agent.list_chats() == []
    with pytest.raises(ValueError, match="не знайдено"):
        agent.open_chat("s")


async def test_user_message_survives_crash(cfg):
    """Повідомлення зберігається ще до відповіді моделі — збій посеред задачі його не губить."""
    agent = Agent(cfg, llm=FakeLLM([RuntimeError("сервер моделі впав")]))
    rec = Recorder()
    with pytest.raises(RuntimeError):
        await agent.run("c", "важливе питання", rec.emit, rec.confirm)
    assert agent.list_chats()[0]["title"] == "важливе питання"


async def test_manual_title_kept_and_long_history_not_trimmed(cfg):
    cfg.max_history_chars = 200
    cfg.summarize = False  # підсумовування — у test_memory.py
    agent = Agent(cfg, llm=FakeLLM([text("а" * 300), text("б")]))
    rec = Recorder()
    await agent.run("c", "перше", rec.emit, rec.confirm)
    agent.rename_chat("c", "Моя назва")
    await agent.run("c", "друге", rec.emit, rec.confirm)
    chat = agent.storage.load_chat("c")
    assert chat["title"] == "Моя назва"
    assert [m["content"] for m in real(chat["messages"]) if m["role"] == "user"] == ["перше", "друге"]  # нічого не обрізано
    # а моделі йде лише те, що влазить у контекст
    assert all(m["content"] != "перше" for m in agent.llm.calls[-1]["messages"])


def test_old_saved_chats_and_sessions_migrated(tmp_path):
    import json
    import sqlite3

    from kru_agent.storage import Storage

    db = tmp_path / "old.sqlite3"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
                               tags TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
        CREATE TABLE sessions (id TEXT PRIMARY KEY, messages TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE saved_chats (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, messages TEXT NOT NULL,
                                  created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO memories(content, tags, created_at) VALUES ('люблю каву', '', 1);
        """
    )
    msgs = json.dumps([{"role": "user", "content": "старе питання"}, {"role": "assistant", "content": "відповідь"}])
    con.execute("INSERT INTO sessions VALUES ('robot-1', ?, 5)", (msgs,))
    con.execute("INSERT INTO saved_chats(title, messages, created_at, updated_at) VALUES ('Збережений', ?, 1, 2)", (msgs,))
    con.commit()
    con.close()

    st = Storage(db)
    assert {(c["id"], c["title"]) for c in st.list_chats()} == {("robot-1", "старе питання"), ("saved-1", "Збережений")}
    assert st.search_memories("каву")[0]["content"] == "люблю каву"
    assert st.list_chats("olena") == [] and st.search_memories("каву", owner="olena") == []
    Storage(db)  # повторне відкриття — міграція не ламається


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
        hello = ws.receive_json()
        assert (hello["user"], hello["role"]) == ("owner", "admin")
        ws.send_json({"type": "task", "text": "привіт", "session_id": "r"})
        _until(ws, "final")
        _until(ws, "status")
        ws.send_json({"type": "chat_list"})
        assert _until(ws, "chat_list")[0]["chats"][0]["title"] == "привіт"  # збережено без жодних дій
        ws.send_json({"type": "chat_rename", "id": "r", "title": "Знайомство"})
        assert _until(ws, "chat_renamed")[0]["title"] == "Знайомство"
        ws.send_json({"type": "chat_open", "id": "r"})
        opened, _ = _until(ws, "chat_opened")
        assert opened["title"] == "Знайомство"
        assert opened["transcript"][-1] == {"role": "assistant", "text": "Привіт! Я Kru."}
        ws.send_json({"type": "chat_export", "id": "r"})
        exp, _ = _until(ws, "chat_export")
        assert "Привіт! Я Kru." in exp["markdown"]
        ws.send_json({"type": "chat_delete", "id": "r"})
        assert _until(ws, "chat_list")[0]["chats"] == []
        ws.send_json({"type": "chat_open", "id": "r"})
        assert "не знайдено" in _until(ws, "chat_error")[0]["message"]


def test_cli_chat_commands():
    from kru_agent.cli import Printer, parse_chat_command

    pr = Printer(False, "cli-1")
    pr({"type": "chat_list", "chats": [{"id": "a", "title": "A", "count": 1, "updated_at": 0},
                                       {"id": "cli-1", "title": "B", "count": 2, "updated_at": 0}]})
    assert parse_chat_command("/open 1", pr)[0] == {"type": "chat_open", "id": "a"}
    assert parse_chat_command("/delete robot-x", pr)[0] == {"type": "chat_delete", "id": "robot-x"}
    assert parse_chat_command("/rename Нова назва", pr)[0] == {"type": "chat_rename", "id": "cli-1", "title": "Нова назва"}
    assert parse_chat_command("/export f.md", pr) == ({"type": "chat_export", "id": "cli-1"}, "f.md")
    assert parse_chat_command("привіт", pr) is None
    pr({"type": "chat_opened", "id": "a", "title": "A", "transcript": []})
    assert pr.session == "a"

