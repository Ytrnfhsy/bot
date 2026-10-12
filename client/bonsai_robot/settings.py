"""Налаштування клієнта: ~/.config/bonsai-robot/config.json (Windows: %APPDATA%\\bonsai-robot\\config.json)"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def config_path() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "bonsai-robot" / "config.json"


def new_session_id() -> str:
    """id нового чату (сервер зберігає кожен чат автоматично)."""
    return "robot-" + uuid.uuid4().hex[:12]


@dataclass
class Settings:
    server_url: str = "ws://127.0.0.1:8765/ws"
    token: str = ""
    session_id: str = field(default_factory=lambda: new_session_id())  # поточний чат
    scale: float = 1.0
    character: str = "robot"  # robot | dog
    pos_x: int | None = None
    pos_y: int | None = None
    sleep_after_sec: int = 180
    show_thinking: bool = False
    last_task: str = ""

    @classmethod
    def load(cls) -> "Settings":
        path = config_path()
        s = cls()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
            except (ValueError, OSError):
                pass
        # змінні середовища мають пріоритет (зручно для першого запуску)
        s.server_url = os.environ.get("BONSAI_SERVER", s.server_url)
        s.token = os.environ.get("BONSAI_TOKEN", s.token)
        s.first_run = not path.is_file()  # перший запуск — одразу покажемо налаштування
        if s.first_run:
            s.save()
        return s

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)  # там токен
        except OSError:
            pass
