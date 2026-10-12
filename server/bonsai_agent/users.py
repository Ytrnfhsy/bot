"""Користувачі агента: кожен токен — окремий користувач зі своєю пам'яттю, чатами та робочою текою.

Файл <DATA_DIR>/users.json зберігає лише SHA-256 токенів, не самі токени.
BONSAI_TOKEN (якщо задано) — це адміністратор «owner»; так старі налаштування працюють як раніше.
Ролі: admin — усі інструменти; user — без run_shell/run_python і лише у своїй теці.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config

OWNER = "owner"
ROLES = ("admin", "user")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# інструменти, які дають доступ до всього сервера, — лише для адміністраторів
ADMIN_ONLY_TOOLS = frozenset({"run_shell", "run_python"})


@dataclass(frozen=True)
class User:
    name: str
    role: str = "admin"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserStore:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.path = config.data_dir / "users.json"

    # ---------- файл ----------
    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Не вдалося прочитати {self.path}: {e}") from e
        return list(data.get("users", []))

    def _save(self, users: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    # ---------- автентифікація ----------
    def authenticate(self, token: str | None) -> User | None:
        """Токен → користувач. Файл читається щоразу, тож `bonsai user add` діє без перезапуску."""
        users = self._load()
        if not self.config.token and not users:
            return User(OWNER, "admin")  # авторизацію не налаштовано — як раніше, один власник
        if not token:
            return None
        if self.config.token and hmac.compare_digest(self.config.token, token):
            return User(OWNER, "admin")
        digest = _hash(token)
        for u in users:
            if hmac.compare_digest(str(u.get("token_sha256", "")), digest):
                return User(u["name"], u.get("role", "user"))
        return None

    # ---------- керування (CLI) ----------
    def list(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in u.items() if k != "token_sha256"} for u in self._load()]

    def add(self, name: str, role: str = "user") -> str:
        """Створює користувача (або видає новий токен наявному) і повертає токен — показати один раз."""
        name = name.strip().lower()
        if not NAME_RE.match(name):
            raise ValueError("Ім'я: латинські літери, цифри, - або _, до 32 символів")
        if role not in ROLES:
            raise ValueError(f"Роль має бути: {', '.join(ROLES)}")
        if name == OWNER and self.config.token:
            raise ValueError("«owner» — це власник із BONSAI_TOKEN; обери інше ім'я")
        token = secrets.token_urlsafe(24)
        users = [u for u in self._load() if u.get("name") != name]
        users.append({"name": name, "role": role, "token_sha256": _hash(token), "created_at": time.time()})
        self._save(users)
        return token

    def revoke(self, name: str) -> bool:
        users = self._load()
        kept = [u for u in users if u.get("name") != name]
        if len(kept) == len(users):
            return False
        self._save(kept)
        return True


def workspace_for(config: Config, user: User) -> Path:
    """Власник працює в BONSAI_WORKSPACE (як і раніше), інші — кожен у своїй теці."""
    if user.name == OWNER:
        return config.workspace
    return config.users_workspace / user.name
