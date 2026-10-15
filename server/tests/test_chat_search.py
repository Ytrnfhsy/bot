"""Пошук по збережених чатах: за назвою й текстом розмови."""

from __future__ import annotations

from fastapi.testclient import TestClient

from kru_agent.agent import Agent
from kru_agent.chats import search
from kru_agent.server import create_app
from kru_agent.users import UserStore

from .conftest import FakeLLM
from .test_server import _until


def _chat(agent, sid, msgs, owner="owner", title=None):
    agent.storage.save_session(sid, msgs, owner)
    if title:
        agent.storage.rename_chat(sid, title, owner)


def _fill(agent):
    _chat(agent, "a", [{"role": "user", "content": "Куди поїхати влітку на МОРЕ?"},
                       {"role": "assistant", "content": "Раджу Одесу або Затоку — там тепле море й піщані пляжі."}])
    _chat(agent, "b", [{"role": "user", "content": "Переклади розділ"},
                       {"role": "assistant", "content": "", "tool_calls": [
                           {"function": {"name": "read_file", "arguments": {"path": "море.txt"}}}]},
                       {"role": "tool", "tool_name": "read_file", "content": "секретне море з файлу"},
                       {"role": "assistant", "content": "Готово."}], title="Переклад книги")
    _chat(agent, "c", [{"role": "user", "content": "привіт"}, {"role": "assistant", "content": "Привіт!"}],
          title="Відпустка на морі")


def test_search_by_text_and_title(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    _fill(agent)
    found = agent.search_chats("море")
    # результати інструментів не шукаються — лише те, що бачить людина; «морі» ≠ «море»
    assert [c["id"] for c in found] == ["a"]
    assert "море" in found[0]["snippet"].casefold()
    assert [c["id"] for c in agent.search_chats("МОР")] == ["c", "a"]  # збіг у назві — вище
    assert [c["id"] for c in agent.search_chats("тепле пляжі")] == ["a"]  # усі слова
    assert agent.search_chats("тепле сніг") == []
    assert agent.search_chats("   ") == []
    assert agent.search_chats("переклад")[0]["id"] == "b"


def test_snippet_is_short_and_centered():
    long = "слово " * 100 + "ЗНАХІДКА тут " + "інше " * 100
    chats = [{"id": "x", "title": "Т", "messages": [{"role": "user", "content": long}], "count": 1,
              "created_at": 0, "updated_at": 0}]
    snip = search(chats, "знахідка")[0]["snippet"]
    assert "ЗНАХІДКА" in snip and snip.startswith("…") and snip.endswith("…") and len(snip) < 160


def test_ws_search_only_own_chats(cfg):
    cfg.token = "secret"
    token = UserStore(cfg).add("olena")
    agent = Agent(cfg, llm=FakeLLM([]))
    _fill(agent)
    _chat(agent, "o", [{"role": "user", "content": "море в Карпатах?"}], owner="olena")
    client = TestClient(create_app(cfg, agent=agent))
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "chat_search", "query": "море"})
        ev, _ = _until(ws, "chat_search")
        assert ev["query"] == "море" and [c["id"] for c in ev["chats"]] == ["a"]
    with client, client.websocket_connect(f"/ws?token={token}") as ws:
        ws.receive_json()
        ws.send_json({"type": "chat_search", "query": "море"})
        assert [c["id"] for c in _until(ws, "chat_search")[0]["chats"]] == ["o"]


def test_cli_search_command(cfg, capsys):
    from kru_agent.cli import Printer, parse_chat_command, run_chat_command_locally

    agent = Agent(cfg, llm=FakeLLM([]))
    _fill(agent)
    printer = Printer(False, "s")
    msg, _ = parse_chat_command("/search тепле море", printer)
    for ev in run_chat_command_locally(agent, msg):
        printer(ev)
    out = capsys.readouterr().out
    assert "1." in out and "тепле море" in out and printer.listed == ["a"]
    assert parse_chat_command("/open 1", printer)[0] == {"type": "chat_open", "id": "a"}
