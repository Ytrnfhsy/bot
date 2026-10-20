"""Налаштування клієнта: ~/.config/kru-robot/config.json (Windows: %APPDATA%\\kru-robot\\config.json)."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _config_base() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"])
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def config_path() -> Path:
    return _config_base() / "kru-robot" / "config.json"


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
    think: bool = True  # модель міркує перед відповіддю (розумніше, але повільніше)
    show_thinking: bool = False
    show_tools: bool = False  # рядки «🔧 read_file: …» — що саме робить агент
    auto_files: bool = False  # запис файлів у робочій теці агента без запиту (копії — в .kru-backup)
    last_task: str = ""
    font_size: int = 13  # шрифт стрічки чату й поля вводу (Ctrl+колесо, Ctrl+=/−/0)
    panel_width: int = 408  # ширина вікна чату (тягнути за край; подвійний клік по краю — як було)
    panel_feed_height: int = 0  # висота стрічки; 0 — під вміст
    voice_reply: str = "voice"  # озвучувати відповіді: off | voice (лише на голосові запити) | always
    voice_volume: int = 90  # гучність озвучення, %
    voice_speaker: int = 0  # голос Piper: 0 — Лада, 1 — Микита, 2 — Тетяна
    voice_effect: str = "robot"  # звучання: natural | robot | digital | droid | mech (див. сервер voice_fx)
    voice_speed: float = 1.0  # темп мовлення

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
        s.server_url = os.environ.get("KRU_SERVER", s.server_url)
        s.token = os.environ.get("KRU_TOKEN", s.token)
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
