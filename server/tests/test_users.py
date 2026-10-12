from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kru_agent.agent import Agent
from kru_agent.server import create_app
from kru_agent.users import User, UserStore

from .conftest import FakeLLM, call, text
from .test_agent import Recorder
from .test_server import _until


def test_user_store(cfg):
    cfg.token = "secret"
    store = UserStore(cfg)
    assert store.authenticate("secret") == User("owner", "admin")
    assert store.authenticate(None) is None and store.authenticate("x") is None

    token = store.add("Olena")
    assert store.authenticate(token) == User("olena", "user")
    assert "token_sha256" not in store.list()[0] and token not in store.path.read_text()
    with pytest.raises(ValueError):
        store.add("bad name!")
    with pytest.raises(ValueError):
        store.add("owner")

    new_token = store.add("olena", "admin")  # новий токен замінює старий
    assert store.authenticate(token) is None and store.authenticate(new_token).role == "admin"
    assert store.revoke("olena") and store.authenticate(new_token) is None and not store.revoke("olena")


def test_no_auth_configured_means_single_owner(cfg):
    store = UserStore(cfg)
    assert store.authenticate(None) == User("owner", "admin")
    store.add("olena")  # щойно з'явились користувачі — без токена не пускаємо
    assert store.authenticate(None) is None


async def test_users_have_separate_chats_memory_and_files(cfg):
    llm = FakeLLM([
        call("remember", content="Олена любить чай"), text("запам'ятала"),
        call("write_file", path="note.txt", content="olena"), text("ok"),
        call("recall", query="чай"), text("нічого"),
    ])
    agent = Agent(cfg, llm=llm)
    olena = User("olena", "user")
    rec = Recorder(approve=True)
    await agent.run("same-id", "запамʼятай", rec.emit, rec.confirm, olena)
    await agent.run("same-id", "створи файл", rec.emit, rec.confirm, olena)
    assert (cfg.users_workspace / "olena" / "note.txt").read_text() == "olena"
    assert not (cfg.workspace / "note.txt").exists()

    await agent.run("same-id", "що я люблю?", rec.emit, rec.confirm)  # власник, той самий id чату
    assert "нічого не знайдено" in llm.calls[-1]["messages"][-1]["content"]
    assert [c["title"] for c in agent.list_chats("owner")] == ["що я люблю?"]
    assert [c["count"] for c in agent.list_chats("olena")] == [2]
    with pytest.raises(ValueError):
        agent.open_chat("same-id", "stranger")


async def test_regular_user_has_no_shell_or_python(cfg):
    llm = FakeLLM([call("run_shell", command="cat /etc/passwd"), text("не можу")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder(approve=True)
    await agent.run("s", "покажи паролі", rec.emit, rec.confirm, User("olena", "user"))
    names = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    assert "run_shell" not in names and "run_python" not in names and "web_search" in names
    assert "недоступний цьому користувачу" in llm.calls[1]["messages"][-1]["content"]
    assert rec.confirms == []  # навіть не питали підтвердження


async def test_user_cannot_escape_workspace_even_if_allowed_for_admin(cfg):
    cfg.allow_outside_workspace = True
    agent = Agent(cfg, llm=FakeLLM([]))
    assert agent.make_context().allow_outside
    ctx = agent.make_context(User("olena", "user"))
    with pytest.raises(Exception, match="поза робочою"):
        ctx.resolve_path("../owner-secret.txt")


def test_ws_per_user_token(cfg):
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM([text("привіт, Олено")]))
    token = UserStore(cfg).add("olena")
    client = TestClient(create_app(cfg, agent=agent))
    with client:
        with client.websocket_connect(f"/ws?token={token}") as ws:
            hello = ws.receive_json()
            assert (hello["user"], hello["role"]) == ("olena", "user")
            ws.send_json({"type": "task", "text": "привіт", "session_id": "c1"})
            _until(ws, "final")
            ws.send_json({"type": "chat_list"})
            assert [c["id"] for c in _until(ws, "chat_list")[0]["chats"]] == ["c1"]
        with client.websocket_connect("/ws?token=secret") as ws:
            ws.receive_json()
            ws.send_json({"type": "chat_list"})
            assert _until(ws, "chat_list")[0]["chats"] == []  # власник не бачить чатів Олени
        UserStore(cfg).revoke("olena")
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"/ws?token={token}") as ws:
                ws.receive_json()
