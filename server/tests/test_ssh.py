"""SSH до віддалених машин: лише зі списку, власним ключем; «без запитів» — тільки на позначених машинах."""

from __future__ import annotations

import pytest

from kru_agent import ssh
from kru_agent.agent import Agent
from kru_agent.cli import main as cli_main
from kru_agent.tools import ToolError
from kru_agent.tools import ssh as ssh_tools
from kru_agent.users import User

from .conftest import FakeLLM, call, text
from .test_agent import Recorder


@pytest.fixture
def runs(monkeypatch):
    """Замість справжнього ssh/scp — запис команд і відповідь, яку задає тест."""
    calls: list[list[str]] = []
    reply = {"out": "[код виходу 0]\nUbuntu 24.04"}

    async def fake_run(cmd, cwd, timeout, limit, shell=False):
        calls.append(cmd)
        return reply["out"]

    monkeypatch.setattr(ssh_tools, "run_process", fake_run)
    return calls, reply


def _key(cfg):
    key = ssh.key_path(cfg)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("PRIVATE")
    key.with_name(key.name + ".pub").write_text("ssh-ed25519 AAAA kru-agent")


def test_hosts_roundtrip_and_validation(cfg):
    assert ssh.load_hosts(cfg) == {}
    ssh.add_host(cfg, "test-vm", "kru@100.92.10.5", confirm=False, note="Ubuntu, можна ламати")
    ssh.add_host(cfg, "prod", "admin@db.netbird.cloud", port=2222)
    hosts = ssh.load_hosts(cfg)
    assert hosts["test-vm"].confirm is False and hosts["prod"].port == 2222 and hosts["prod"].confirm is True
    assert "без запитів" in hosts["test-vm"].describe() and "db.netbird.cloud:2222" in hosts["prod"].describe()
    for name, target in (("x y", "kru@h"), ("ok", "no-user-host"), ("ok", "a@b; rm -rf /"), ("ok", "-oProxy=x@h")):
        with pytest.raises(ssh.SSHError):
            ssh.add_host(cfg, name, target)
    assert ssh.remove_host(cfg, "prod") and not ssh.remove_host(cfg, "prod")
    assert list(ssh.load_hosts(cfg)) == ["test-vm"]


def test_ssh_command_is_locked_down(cfg):
    host = ssh.Host("vm", "kru@100.92.10.5", 2222)
    cmd = ssh.ssh_command(cfg, host, "uname -a")
    assert cmd[0] == "ssh" and cmd[-3:] == ["kru@100.92.10.5", "--", "uname -a"]
    opts = " ".join(cmd)
    for must in ("-F /dev/null", "IdentitiesOnly=yes", "BatchMode=yes", "StrictHostKeyChecking=accept-new",
                 str(ssh.key_path(cfg)), str(ssh.ssh_dir(cfg) / "known_hosts"), "-p 2222", "-n"):
        assert must in opts, must
    scp = ssh.scp_command(cfg, host, "/w/a.sh", "kru@100.92.10.5:/tmp/a.sh")
    assert scp[0] == "scp" and "-P" in scp and scp[-2:] == ["/w/a.sh", "kru@100.92.10.5:/tmp/a.sh"]


async def test_trusted_vm_runs_without_asking(cfg, runs):
    calls, _ = runs
    _key(cfg)
    ssh.add_host(cfg, "test-vm", "kru@100.92.10.5", confirm=False, note="тестова ВМ")
    llm = FakeLLM([call("ssh_run", host="test-vm", command="lsb_release -d"), text("Там Ubuntu 24.04")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    assert await agent.run("s", "що за система на ВМ?", rec.emit, rec.confirm) == "Там Ubuntu 24.04"
    assert rec.confirms == []  # машина позначена «без запитів»
    assert calls[0][-3:] == ["kru@100.92.10.5", "--", "lsb_release -d"]
    system = llm.calls[0]["messages"][0]["content"]
    assert "test-vm (kru@100.92.10.5, без запитів) — тестова ВМ" in system and "sudo -n" in system
    assert "Ubuntu 24.04" in llm.calls[1]["messages"][-1]["content"]


async def test_other_hosts_ask_and_users_have_no_ssh(cfg, runs):
    calls, _ = runs
    _key(cfg)
    ssh.add_host(cfg, "prod", "admin@10.0.0.5")  # confirm за замовчуванням
    agent = Agent(cfg, llm=FakeLLM([call("ssh_run", host="prod", command="reboot"), text("Не став")]))
    rec = Recorder(approve=False)
    await agent.run("s", "перезавантаж", rec.emit, rec.confirm)
    assert rec.confirms == ["ssh_run"] and calls == []
    ctx = agent.make_context(User("olena", "user"))
    assert {"ssh_run", "ssh_upload", "ssh_download"} <= ctx.denied_tools
    with pytest.raises(ToolError, match="недоступний"):
        await agent.registry.call(ctx, "ssh_run", {"host": "prod", "command": "id"})
    llm = FakeLLM([text("Не можу")])
    await Agent(cfg, llm=llm).run("o", "зайди на prod", rec.emit, rec.confirm, User("olena", "user"))
    assert "Remote machines" not in llm.calls[0]["messages"][0]["content"]  # чужим машин навіть не показуємо


async def test_errors_are_explained(cfg, runs):
    calls, reply = runs
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    with pytest.raises(ToolError, match="kru ssh add"):
        await agent.registry.call(ctx, "ssh_run", {"host": "vm", "command": "id"})
    ssh.add_host(cfg, "vm", "kru@100.92.10.5", confirm=False)
    _key(cfg)
    with pytest.raises(ToolError, match="Доступні: vm"):
        await agent.registry.call(ctx, "ssh_run", {"host": "інша", "command": "id"})
    reply["out"] = "[код виходу 255]\nkru@100.92.10.5: Permission denied (publickey)."
    out = await agent.registry.call(ctx, "ssh_run", {"host": "vm", "command": "id"})
    assert "ключ агента не додано" in out
    reply["out"] = "[код виходу 255]\nssh: connect to host 100.92.10.5 port 22: Connection timed out"
    assert "NetBird" in await agent.registry.call(ctx, "ssh_run", {"host": "vm", "command": "id"})
    reply["out"] = "[код виходу 1]\nPermission denied"  # помилка самої команди — без підказок про ssh
    assert "SSH:" not in await agent.registry.call(ctx, "ssh_run", {"host": "vm", "command": "cat /root/x"})


async def test_upload_download(cfg, runs):
    calls, reply = runs
    _key(cfg)
    ssh.add_host(cfg, "vm", "kru@100.92.10.5", confirm=False)
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    (cfg.workspace / "deploy.sh").write_text("echo hi")
    out = await agent.registry.call(ctx, "ssh_upload", {"host": "vm", "path": "deploy.sh", "remote_path": "/tmp/"})
    assert out == "Передано deploy.sh → vm:/tmp/" and calls[-1][-2:] == [str(cfg.workspace / "deploy.sh"),
                                                                      "kru@100.92.10.5:/tmp/"]
    with pytest.raises(ToolError, match="поза робочою"):
        await agent.registry.call(ctx, "ssh_upload", {"host": "vm", "path": "/etc/shadow", "remote_path": "/tmp/"})
    out = await agent.registry.call(ctx, "ssh_download", {"host": "vm", "remote_path": "/var/log/syslog"})
    assert out == "Завантажено vm:/var/log/syslog → from-vm/syslog"
    assert calls[-1][-2:] == ["kru@100.92.10.5:/var/log/syslog", str(cfg.workspace / "from-vm" / "syslog")]
    reply["out"] = "[код виходу 1]\nscp: /nope: No such file or directory"
    assert "No such file" in await agent.registry.call(ctx, "ssh_download", {"host": "vm", "remote_path": "/nope"})


def test_cli_add_list_remove(cfg, monkeypatch, capsys):
    monkeypatch.setenv("KRU_DATA_DIR", str(cfg.data_dir))
    monkeypatch.setattr("kru_agent.config._load_dotenv", lambda path: None)
    monkeypatch.setattr(ssh, "ensure_key", lambda c: (_key(c), ssh.key_path(c).with_suffix(".pub"))[1])
    monkeypatch.setattr("kru_agent.cli._ssh_ok", lambda *a, **k: False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    cli_main(["ssh", "add", "test-vm", "kru@100.92.10.5", "--no-confirm", "--note", "Ubuntu"])
    out = capsys.readouterr().out
    assert "test-vm (kru@100.92.10.5, без запитів) — Ubuntu" in out and "/home/kru/.ssh/authorized_keys" in out
    assert "ssh-ed25519 AAAA kru-agent" in out  # команда для authorized_keys на самій ВМ
    cli_main(["ssh", "list"])
    assert "test-vm" in capsys.readouterr().out
    cli_main(["ssh", "remove", "test-vm"])
    assert ssh.load_hosts(cfg) == {}
    with pytest.raises(SystemExit, match="користувач@адреса"):
        cli_main(["ssh", "add", "vm", "100.92.10.5"])



async def test_scp_falls_back_to_legacy_protocol(cfg, monkeypatch):
    """Немає SFTP на машині (мінімальні образи) — повтор через scp -O."""
    _key(cfg)
    ssh.add_host(cfg, "vm", "kru@100.92.10.5", confirm=False)
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    (cfg.workspace / "a.txt").write_text("x")
    calls = []
    replies = iter(["[код виходу 255]\nsubsystem request failed on channel 0\nscp: Connection closed",
                    "[код виходу 0]\n(порожній вивід)",
                    "[ТАЙМАУТ після 5 с — процес зупинено]\n"])

    async def fake_run(cmd, cwd, timeout, limit, shell=False):
        calls.append(cmd)
        return next(replies)

    monkeypatch.setattr(ssh_tools, "run_process", fake_run)
    out = await agent.registry.call(ctx, "ssh_upload", {"host": "vm", "path": "a.txt", "remote_path": "/tmp/"})
    assert out.startswith("Передано") and calls[0][1] != "-O" and calls[1][:2] == ["scp", "-O"]
    out = await agent.registry.call(ctx, "ssh_run", {"host": "vm", "command": "sleep 99", "timeout": 5})
    assert "nohup" in out  # після тайм-ауту команда на машині могла лишитися працювати
