"""Файли від агента одним кліком: send_file → кнопка в чаті → file_get → файл на ПК (у Telegram — документом)."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from kru_agent.agent import Agent
from kru_agent.server import create_app
from kru_agent.telegram import TelegramBot
from kru_agent.tools import ToolError
from kru_agent.tools.files import MAX_SHARE, human_size
from kru_agent.users import User

from .conftest import FakeLLM, call, text
from .test_agent import Recorder
from .test_server import _until
from .test_telegram_bot import CHAT, make, msg


async def test_send_file_emits_event_and_shows_in_transcript(cfg):
    (cfg.workspace / "reports").mkdir(parents=True)
    (cfg.workspace / "reports" / "звіт.md").write_text("# Звіт\n" * 100, encoding="utf-8")
    llm = FakeLLM([call("send_file", path="reports/звіт.md"), call("send_file", path="нема.pdf"), text("Готово")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "зроби звіт", rec.emit, rec.confirm)
    files = [e for e in rec.events if e["type"] == "file"]
    assert files == [{"type": "file", "path": "reports/звіт.md", "name": "звіт.md", "size": 1100}]
    assert rec.confirms == []  # передати файл самому користувачу — без запиту дозволу
    assert "send_file" in llm.calls[0]["messages"][0]["content"]
    # після відкриття чату кнопка лишається (а невдалої спроби немає)
    shown = [i for i in agent.open_chat("s")["transcript"] if i.get("file")]
    assert shown == [{"role": "tool", "text": "📎 Файл: звіт.md", "file": {"path": "reports/звіт.md", "name": "звіт.md"}}]


async def test_send_file_limits(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    (cfg.workspace / "dir").mkdir(parents=True)
    with pytest.raises(ToolError, match="zip"):
        await agent.registry.call(ctx, "send_file", {"path": "dir"})
    with pytest.raises(ToolError, match="поза робочою"):
        await agent.registry.call(ctx, "send_file", {"path": "../../etc/passwd"})
    big = cfg.workspace / "big.bin"
    with big.open("wb") as f:
        f.truncate(MAX_SHARE + 1)
    with pytest.raises(ToolError, match="завеликий"):
        await agent.registry.call(ctx, "send_file", {"path": "big.bin"})
    assert human_size(1536) == "1,5 КБ" and human_size(10 * 1024 * 1024) == "10 МБ"


def test_ws_download_file(cfg):
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM([call("send_file", path="out/data.csv"), text("Ось таблиця")]))
    (cfg.workspace / "out").mkdir(parents=True)
    (cfg.workspace / "out" / "data.csv").write_bytes(b"a,b\n1,2\n")
    client = TestClient(create_app(cfg, agent=agent))
    with client, client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "task", "text": "зроби таблицю", "session_id": "t"})
        ev, _ = _until(ws, "file")
        assert ev["name"] == "data.csv" and ev["size"] == 8
        _until(ws, "final")
        ws.send_json({"type": "file_get", "id": "f1", "path": ev["path"]})
        got, _ = _until(ws, "file_data")
        assert got["id"] == "f1" and got["name"] == "data.csv" and base64.b64decode(got["data"]) == b"a,b\n1,2\n"
        ws.send_json({"type": "file_get", "id": "f2", "path": "../../../etc/passwd"})
        err, _ = _until(ws, "file_error")
        assert err["id"] == "f2" and "поза робочою" in err["message"]
        ws.send_json({"type": "file_get", "id": "f3", "path": "зник.txt"})
        assert "не знайдено" in _until(ws, "file_error")[0]["message"]


def test_regular_user_downloads_only_own_files(cfg):
    from kru_agent.users import UserStore

    cfg.token = "secret"
    token = UserStore(cfg).add("olena")
    (cfg.workspace / "secret.txt").write_text("власника")
    olena_ws = cfg.users_workspace / "olena"
    olena_ws.mkdir(parents=True)
    (olena_ws / "мій.txt").write_text("її")
    client = TestClient(create_app(cfg, agent=Agent(cfg, llm=FakeLLM([]))))
    with client, client.websocket_connect(f"/ws?token={token}") as ws:
        assert ws.receive_json()["user"] == "olena"
        ws.send_json({"type": "file_get", "id": "1", "path": "мій.txt"})
        assert base64.b64decode(_until(ws, "file_data")[0]["data"]).decode() == "її"
        ws.send_json({"type": "file_get", "id": "2", "path": str(cfg.workspace / "secret.txt")})
        assert _until(ws, "file_error")[0]["id"] == "2"


async def test_telegram_gets_document(cfg):
    gw, agent, bot = make(cfg, [call("send_file", path="звіт.txt"), text("Надіслав звіт")])
    (cfg.workspace / "звіт.txt").write_text("дані")
    docs = []

    async def send_document(chat_id, path, caption=""):
        docs.append((str(chat_id), path.name, path.read_text()))

    bot.send_document = send_document
    await gw.handle(msg("надішли звіт"))
    assert docs == [(CHAT, "звіт.txt", "дані")] and bot.texts()[-1] == "Надіслав звіт"


async def test_send_telegram_with_attachment(cfg, monkeypatch):
    calls = []

    async def fake_call(self, method, **params):
        calls.append(("message", params["text"]))
        return {"message_id": 5}

    async def fake_doc(self, chat_id, path, caption=""):
        calls.append(("document", path.name, caption))

    monkeypatch.setattr(TelegramBot, "_call", fake_call)
    monkeypatch.setattr(TelegramBot, "send_document", fake_doc)
    cfg.telegram_bot_token = "t"
    agent = Agent(cfg, llm=FakeLLM([]))
    agent.storage.set_telegram_chat("owner", "555")
    ctx = agent.make_context()
    (cfg.workspace / "a.pdf").write_bytes(b"%PDF")
    out = await agent.registry.call(ctx, "send_telegram", {"text": "Звіт за тиждень", "path": "a.pdf"})
    assert "a.pdf" in out and calls == [("document", "a.pdf", "Звіт за тиждень")]
    calls.clear()
    long_text = "слово " * 400  # задовгий для підпису — окремим повідомленням перед файлом
    await agent.registry.call(ctx, "send_telegram", {"text": long_text, "path": "a.pdf"})
    assert [c[0] for c in calls] == ["message", "document"] and calls[1][2] == ""
    assert await agent.registry.call(ctx, "send_telegram", {"text": "x " * 3000}) == "Надіслано в Telegram (2 повідомлення)"
    with pytest.raises(ToolError, match="не знайдено"):
        await agent.registry.call(ctx, "send_telegram", {"text": "x", "path": "нема.pdf"})
    assert agent.make_context(User("olena", "user")).denied_tools.isdisjoint({"send_file"})


def test_transcript_json_mode_file():
    from kru_agent.chats import transcript

    msgs = [
        {"role": "user", "content": "дай файл"},
        {"role": "assistant", "content": '```json\n{"tool": "send_file", "args": {"path": "./a/b.zip"}}\n```'},
        {"role": "user", "content": "TOOL RESULT (send_file):\nКористувач отримав кнопку завантаження файлу b.zip (1 КБ)."},
        {"role": "assistant", "content": "Ось архів"},
    ]
    out = transcript(msgs)
    assert out[1] == {"role": "tool", "text": "📎 Файл: b.zip", "file": {"path": "./a/b.zip", "name": "b.zip"}}
    assert [i["role"] for i in out] == ["user", "tool", "assistant"]
