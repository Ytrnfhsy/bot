from __future__ import annotations

from kru_agent.agent import Agent

from .conftest import FakeLLM, call, text
from .test_agent import Recorder
from .test_server import _client, _until


class TaskScopeRecorder(Recorder):
    """Відповідає «дозволяти до кінця задачі» на запис файлів."""

    async def confirm(self, tool, summary, args):
        self.confirms.append(tool)
        return "task" if tool in {"write_file", "edit_file"} else True


async def test_auto_files_writes_without_asking_and_keeps_backup(cfg):
    (cfg.workspace / "book.txt").write_text("оригінал", encoding="utf-8")
    llm = FakeLLM([
        call("write_file", path="out/part1.txt", content="переклад 1"),
        call("write_file", path="book.txt", content="новий текст"),
        call("edit_file", path="book.txt", old_text="новий", new_text="готовий"),
        call("run_shell", command="echo hi"),
        text("готово"),
    ])
    agent = Agent(cfg, llm=llm)
    rec = Recorder(approve=True)
    await agent.run("s", "переклади книгу", rec.emit, rec.confirm, auto_files=True)

    assert rec.confirms == ["run_shell"]  # файли — без запитів, shell — як завжди
    assert (cfg.workspace / "out/part1.txt").read_text(encoding="utf-8") == "переклад 1"
    assert (cfg.workspace / "book.txt").read_text(encoding="utf-8") == "готовий текст"
    backups = sorted((cfg.workspace / ".kru-backup").rglob("book.txt"))
    assert {b.read_text(encoding="utf-8") for b in backups} == {"оригінал", "новий текст"}
    assert "попередня версія — .kru-backup/" in llm.calls[2]["messages"][-1]["content"]


async def test_auto_files_never_outside_workspace(cfg, tmp_path):
    cfg.allow_outside_workspace = True  # адміну дозволено виходити за межі теки — але без запиту не можна
    outside = tmp_path / "system.conf"
    llm = FakeLLM([call("write_file", path=str(outside), content="x"), text("ок")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder(approve=False)
    await agent.run("s", "запиши", rec.emit, rec.confirm, auto_files=True)
    assert rec.confirms == ["write_file"] and not outside.exists()


async def test_allow_for_rest_of_task(cfg):
    llm = FakeLLM([
        call("write_file", path="a.txt", content="1"),
        call("write_file", path="b.txt", content="2"),
        call("edit_file", path="a.txt", old_text="1", new_text="11"),
        call("run_shell", command="ls"),
        call("run_shell", command="ls"),
        text("ок"),
    ])
    agent = Agent(cfg, llm=llm)
    rec = TaskScopeRecorder()
    await agent.run("s", "зроби", rec.emit, rec.confirm)
    # write_file спитали раз, edit_file — окремий інструмент, теж раз; shell — щоразу
    assert rec.confirms == ["write_file", "edit_file", "run_shell", "run_shell"]

    # наступна задача знову питає
    llm.script += [call("write_file", path="c.txt", content="3"), text("ок")]
    rec2 = Recorder()
    await agent.run("s", "ще", rec2.emit, rec2.confirm)
    assert rec2.confirms == ["write_file"]


def test_ws_scope_task_and_auto_files(cfg):
    client, agent = _client(cfg, [
        call("write_file", path="a.txt", content="1"), call("write_file", path="b.txt", content="2"), text("ок"),
        call("write_file", path="c.txt", content="3"), text("ок2"),
    ])
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "task", "text": "пиши", "session_id": "t"})
        req, _ = _until(ws, "confirm_request")
        ws.send_json({"type": "confirm_response", "id": req["id"], "approved": True, "scope": "task"})
        final, seen = _until(ws, "final")
        assert final["text"] == "ок" and not any(e["type"] == "confirm_request" for e in seen)

        ws.send_json({"type": "task", "text": "ще", "session_id": "t", "auto_files": True})
        final, seen = _until(ws, "final")
        assert final["text"] == "ок2" and not any(e["type"] == "confirm_request" for e in seen)
    assert (cfg.workspace / "c.txt").read_text() == "3"


def test_prompt_prefers_file_tools_over_shell():
    from kru_agent.agent import SYSTEM_PROMPT

    assert "never run_shell" in SYSTEM_PROMPT
