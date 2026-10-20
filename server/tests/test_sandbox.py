"""Пісочниця: run_shell / run_python у Docker — без запитів дозволу, і для звичайних користувачів."""

from __future__ import annotations

import pytest

from kru_agent import sandbox
from kru_agent.agent import Agent
from kru_agent.tools import ToolError
from kru_agent.users import User

from .conftest import FakeLLM, call, text
from .test_agent import Recorder


@pytest.fixture
def sbx(cfg, monkeypatch):
    """Увімкнена пісочниця; замість docker — запис команд."""
    cfg.sandbox = "docker"
    runs = []

    async def fake_run_process(cmd, cwd, timeout, limit, shell=False):
        runs.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return "[код виходу 0]\nok"

    monkeypatch.setattr("kru_agent.tools.shell.run_process", fake_run_process)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")

    async def no_remove(name):
        pass

    monkeypatch.setattr(sandbox, "_remove", no_remove)
    return runs


def test_docker_command_is_isolated(cfg):
    cfg.sandbox = "docker"
    ctx = Agent(cfg, llm=FakeLLM([])).make_context()
    cmd = sandbox.docker_command(ctx, ["python", "-V"], "kru-sbx-1")
    joined = " ".join(cmd)
    assert cmd[:2] == ["docker", "run"] and cmd[-2:] == ["python", "-V"]
    for flag in ("--rm", "--read-only", "--cap-drop ALL", "no-new-privileges", "--pids-limit 256",
                 "--memory 2g", "--pull never", f"{ctx.workspace.resolve()}:{ctx.workspace.resolve()}"):
        assert flag in joined
    assert "--privileged" not in joined and "/var/run/docker.sock" not in joined


async def test_sandbox_runs_without_confirmation(cfg, sbx):
    llm = FakeLLM([call("run_shell", command="ls -la"), call("run_python", code="print(1)"), text("готово")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "порахуй", rec.emit, rec.confirm)
    assert rec.confirms == []  # у пісочниці дозвіл не питається
    shell_cmd, py_cmd = sbx[0]["cmd"], sbx[1]["cmd"]
    assert shell_cmd[-3:] == ["bash", "-lc", "ls -la"] and "timeout" in shell_cmd
    # тека в контейнері — за тим самим шляхом, що й на сервері
    ws = str(cfg.workspace.resolve())
    assert py_cmd[-3] == "python" and py_cmd[-1].startswith(f"{ws}/.kru_tmp/") and py_cmd[-1].endswith(".py")
    assert py_cmd[py_cmd.index("-w") + 1] == ws
    assert "SANDBOX" in llm.calls[0]["messages"][0]["content"]


async def test_sandbox_confirm_option(cfg, sbx):
    cfg.sandbox_confirm = True
    agent = Agent(cfg, llm=FakeLLM([call("run_shell", command="ls"), text("ok")]))
    rec = Recorder()
    await agent.run("s", "ls", rec.emit, rec.confirm)
    assert rec.confirms == ["run_shell"]


async def test_regular_user_gets_shell_in_sandbox(cfg, sbx):
    llm = FakeLLM([call("run_python", code="print(2+2)"), text("4")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "2+2", rec.emit, rec.confirm, User("olena", "user"))
    names = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    assert "run_python" in names and "run_shell" in names
    assert str(cfg.users_workspace / "olena") in " ".join(sbx[0]["cmd"])  # лише її тека


async def test_without_sandbox_still_asks_and_regular_users_denied(cfg):
    llm = FakeLLM([call("run_shell", command="echo hi"), text("ok")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder(approve=False)
    await agent.run("s", "echo", rec.emit, rec.confirm)
    assert rec.confirms == ["run_shell"]
    assert "require user approval" in llm.calls[0]["messages"][0]["content"]
    assert agent.make_context(User("olena", "user")).denied_tools == {"run_shell", "run_python", "ssh_run",
                                                                     "ssh_upload", "ssh_download"}


async def test_timeout_and_missing_image_messages(cfg, monkeypatch):
    cfg.sandbox = "docker"
    ctx = Agent(cfg, llm=FakeLLM([])).make_context()
    outputs = iter(["[код виходу 124]\nчастковий вивід",
                    "[код виходу 125]\nUnable to find image 'kru-sandbox:latest' locally\nNo such image"])

    async def fake_run_process(cmd, cwd, timeout, limit, shell=False):
        return next(outputs)

    async def no_remove(name):
        pass

    monkeypatch.setattr("kru_agent.tools.shell.run_process", fake_run_process)
    monkeypatch.setattr(sandbox, "_remove", no_remove)
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    out = await sandbox.run(ctx, ["sleep", "99"], 3)
    assert out.startswith("[ТАЙМАУТ після 3 с") and "частковий вивід" in out
    with pytest.raises(ToolError, match="deploy/sandbox.sh build"):
        await sandbox.run(ctx, ["ls"], 3)


async def test_no_docker_is_an_error_not_host_fallback(cfg, monkeypatch):
    cfg.sandbox = "docker"
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    agent = Agent(cfg, llm=FakeLLM([]))
    with pytest.raises(ToolError, match="немає docker"):
        await agent.registry.call(agent.make_context(), "run_shell", {"command": "rm -rf ~"})


async def test_legacy_workspace_paths_resolve(cfg):
    """Шляхи /workspace/… (так тека звалась у пісочниці раніше) — це файли робочої теки, а не «поза нею»."""
    cfg.sandbox = "docker"
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    (cfg.workspace / "img").mkdir(parents=True)
    (cfg.workspace / "img" / "a.png").write_bytes(b"x")
    assert ctx.resolve_path("/workspace/img/a.png") == (cfg.workspace / "img" / "a.png").resolve()
    assert ctx.resolve_path("/workspace") == cfg.workspace.resolve()
    out = await agent.registry.call(ctx, "read_file", {"path": "/workspace/img/a.png"})
    assert "ПОМИЛКА" not in out
