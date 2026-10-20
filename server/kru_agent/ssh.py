"""SSH-доступ агента до віддалених машин (наприклад, тестової ВМ у NetBird).

Агент підключається лише до машин зі списку <data_dir>/ssh/hosts.json і лише власним ключем
<data_dir>/ssh/id_ed25519 — пароль чи довільну адресу йому не підсунути. Керування — `kru ssh …`.

hosts.json: {"test-vm": {"target": "kru@100.92.10.5", "port": 22, "confirm": false, "note": "…"}}
  confirm=false — команди на цій машині виконуються без запиту дозволу (тестове середовище).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,40}$")
TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.:_-]+$")


class SSHError(Exception):
    pass


@dataclass
class Host:
    name: str
    target: str  # користувач@адреса
    port: int = 22
    confirm: bool = True  # питати дозволу на кожну дію
    note: str = ""  # що це за машина (бачить агент)

    def describe(self) -> str:
        how = "питає дозволу" if self.confirm else "без запитів"
        note = f" — {self.note}" if self.note else ""
        return f"{self.name} ({self.target}{'' if self.port == 22 else f':{self.port}'}, {how}){note}"


def ssh_dir(config) -> Path:
    return Path(config.data_dir) / "ssh"


def key_path(config) -> Path:
    return ssh_dir(config) / "id_ed25519"


def _hosts_file(config) -> Path:
    return ssh_dir(config) / "hosts.json"


def load_hosts(config) -> dict[str, Host]:
    path = _hosts_file(config)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    hosts = {}
    for name, item in raw.items() if isinstance(raw, dict) else ():
        if isinstance(item, dict) and NAME_RE.match(name) and TARGET_RE.match(str(item.get("target", ""))):
            hosts[name] = Host(name, str(item["target"]), int(item.get("port") or 22),
                               bool(item.get("confirm", True)), str(item.get("note") or ""))
    return hosts


def save_hosts(config, hosts: dict[str, Host]) -> None:
    path = _hosts_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {h.name: {k: v for k, v in asdict(h).items() if k != "name"} for h in hosts.values()}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_host(config, name: str, target: str, port: int = 22, confirm: bool = True, note: str = "") -> Host:
    if not NAME_RE.match(name):
        raise SSHError("Назва: латиниця, цифри, . _ - (наприклад, test-vm)")
    if not TARGET_RE.match(target):
        raise SSHError("Адреса має бути у вигляді користувач@адреса, наприклад kru@100.92.10.5")
    if not 0 < port < 65536:
        raise SSHError("Порт 1–65535")
    hosts = load_hosts(config)
    hosts[name] = Host(name, target, port, confirm, note)
    save_hosts(config, hosts)
    return hosts[name]


def remove_host(config, name: str) -> bool:
    hosts = load_hosts(config)
    if hosts.pop(name, None) is None:
        return False
    save_hosts(config, hosts)
    return True


def ensure_key(config) -> Path:
    """Окремий ключ агента (без пароля): створюється один раз. Повертає шлях до .pub."""
    key = key_path(config)
    if not key.is_file():
        key.parent.mkdir(parents=True, exist_ok=True)
        key.parent.chmod(0o700)
        try:
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"{config.agent_name.lower()}-agent",
                            "-f", str(key)], check=True, capture_output=True)
        except FileNotFoundError as e:
            raise SSHError("Не знайдено ssh-keygen — встанови OpenSSH-клієнт (sudo dnf install openssh-clients)") from e
        except subprocess.CalledProcessError as e:
            raise SSHError(f"ssh-keygen: {e.stderr.decode(errors='replace').strip()}") from e
    return key.with_name(key.name + ".pub")


def common_options(config) -> list[str]:
    """Спільні параметри ssh/scp: лише ключ агента, без запитів пароля, відомі хости — окремо від користувача."""
    d = ssh_dir(config)
    return [
        "-F", "/dev/null",  # не чіпати ~/.ssh/config власника
        "-i", str(key_path(config)),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",  # ніяких інтерактивних запитів (пароль, «yes/no»)
        "-o", "StrictHostKeyChecking=accept-new",  # перший раз запам'ятати ключ машини, далі — перевіряти
        "-o", f"UserKnownHostsFile={d / 'known_hosts'}",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=4",
    ]


def ssh_command(config, host: Host, command: str) -> list[str]:
    return ["ssh", *common_options(config), "-p", str(host.port), "-n", host.target, "--", command]


def scp_command(config, host: Host, src: str, dst: str, recursive: bool = False) -> list[str]:
    return ["scp", *common_options(config), "-P", str(host.port), *(["-r"] if recursive else []), src, dst]


def prompt_section(config) -> str:
    """Рядки для системного промпту: які машини доступні."""
    hosts = load_hosts(config)
    if not hosts:
        return ""
    lines = "\n".join(f"  · {h.describe()}" for h in hosts.values())
    return (
        "- Remote machines over SSH (ssh_run / ssh_upload / ssh_download; host = the name):\n" + lines + "\n"
        "  Commands run non-interactively: use `sudo -n`, `DEBIAN_FRONTEND=noninteractive apt-get -y …`, `--yes` flags; "
        "for long jobs use `nohup … &` or systemd and check back. Machines marked «без запитів» are the user's test "
        "environment: experiment freely there, but say what you changed.\n"
    )
