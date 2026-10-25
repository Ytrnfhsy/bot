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

    async def send_file(chat_id, path, caption=""):
        docs.append((str(chat_id), path.name, path.read_text()))

    bot.send_file = send_file
    await gw.handle(msg("надішли звіт"))
    assert docs == [(CHAT, "звіт.txt", "дані")] and bot.texts()[-1] == "Надіслав звіт"


async def test_send_telegram_with_attachment(cfg, monkeypatch):
    calls = []

    async def fake_call(self, method, **params):
        calls.append(("message", params["text"]))
        return {"message_id": 5}

    async def fake_doc(self, chat_id, path, caption="", kind="document"):
        calls.append((kind, path.name, caption))

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


async def test_telegram_images_go_as_photo(monkeypatch, tmp_path):
    from kru_agent.telegram import TelegramError

    sent = []

    async def fake_doc(self, chat_id, path, caption="", kind="document"):
        sent.append((kind, path.name))
        if kind == "photo" and path.name == "weird.png":
            raise TelegramError("PHOTO_INVALID_DIMENSIONS")

    monkeypatch.setattr(TelegramBot, "send_document", fake_doc)
    bot = TelegramBot("t")
    for name in ("a.jpg", "weird.png", "b.pdf"):
        (tmp_path / name).write_bytes(b"x")
        await bot.send_file("1", tmp_path / name)
    assert sent == [("photo", "a.jpg"), ("photo", "weird.png"), ("document", "weird.png"), ("document", "b.pdf")]


def _mock_http(monkeypatch, handler):
    import httpx

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


async def test_download_url(cfg, monkeypatch):
    import httpx

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/big"):
            return httpx.Response(200, headers={"content-length": str(MAX_SHARE + 1)}, content=b"")
        if request.url.path.endswith("/page"):
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html></html>")
        assert "Mozilla" in request.headers["user-agent"]  # Wikimedia відхиляє запити без нормального User-Agent
        return httpx.Response(200, headers={"content-type": "image/png"}, content=png)

    _mock_http(monkeypatch, handler)
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    url = "https://upload.wikimedia.org/wikipedia/commons/4/48/Volodymyr_Zelenskyy_in_2024.png"
    out = await agent.registry.call(ctx, "download_url", {"url": url})
    assert out.startswith("Завантажено: downloads/Volodymyr_Zelenskyy_in_2024.png")
    assert (cfg.workspace / "downloads" / "Volodymyr_Zelenskyy_in_2024.png").read_bytes() == png
    out = await agent.registry.call(ctx, "download_url", {"url": url})  # не перезаписує
    assert "Volodymyr_Zelenskyy_in_2024-2.png" in out
    out = await agent.registry.call(ctx, "download_url", {"url": "https://x.org/img?id=5", "path": "pics/"})
    assert "pics/img.png" in out
    assert "HTML-сторінку" in await agent.registry.call(ctx, "download_url", {"url": "https://x.org/page"})
    with pytest.raises(ToolError, match="50 МБ"):
        await agent.registry.call(ctx, "download_url", {"url": "https://x.org/big"})
    with pytest.raises(ToolError, match="поза робочою"):
        await agent.registry.call(ctx, "download_url", {"url": url, "path": "../../x.png"})
    with pytest.raises(ToolError, match="download_url"):
        await agent.registry.call(ctx, "fetch_url", {"url": url})
    # і далі — одразу в Telegram / на ПК
    assert (await agent.registry.call(ctx, "send_file", {"path": "downloads/Volodymyr_Zelenskyy_in_2024.png"})
            ).startswith("Файл")


async def test_web_tools_do_not_reach_local_network(cfg, monkeypatch):
    """Сторінка з «підказкою» чи редірект не відкриють моделі llama-server, HolosTTS, машини NetBird."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8085/v1/models"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"SECRET")

    _mock_http(monkeypatch, handler)
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    for url in ("http://127.0.0.1:8085/v1/models", "http://192.168.1.10/", "http://100.80.1.2:8890/",
                "http://[::1]:8086/", "http://169.254.169.254/latest", "https://evil.example/r"):
        for tool in ("fetch_url", "download_url"):
            with pytest.raises(ToolError, match="локальної мережі"):
                await agent.registry.call(ctx, tool, {"url": url})
    assert "SECRET" in await agent.registry.call(ctx, "fetch_url", {"url": "https://example.org/"})
    cfg.allow_private_urls = True  # власник дозволив свідомо
    assert "SECRET" in await agent.registry.call(ctx, "fetch_url", {"url": "http://192.168.1.10/"})


async def test_backup_not_written_through_symlink(cfg, tmp_path):
    """.kru-backup, підмінений посиланням назовні (shell у пісочниці), не стає дорогою за межі робочої теки."""
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "old").mkdir()
    import os
    os.utime(outside / "old", (0, 0))  # «старша за 7 днів» — прибирання видалило б її
    (cfg.workspace / "a.txt").write_text("1")
    (cfg.workspace / ".kru-backup").symlink_to(outside, target_is_directory=True)
    await agent.registry.call(ctx, "write_file", {"path": "a.txt", "content": "2"})
    assert (cfg.workspace / "a.txt").read_text() == "2"
    assert sorted(p.name for p in outside.iterdir()) == ["old"]


async def test_list_dir_recursive_is_bounded(cfg, monkeypatch):
    from kru_agent.tools import files

    for i in range(30):
        (cfg.workspace / f"d{i}").mkdir()
    monkeypatch.setattr(files, "LIST_MAX_DIRS", 10)
    agent = Agent(cfg, llm=FakeLLM([]))
    out = await agent.registry.call(agent.make_context(), "list_dir", {"path": ".", "recursive": True, "pattern": "*.nope"})
    assert "переглянуто 10 тек" in out
