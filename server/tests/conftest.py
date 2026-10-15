from __future__ import annotations

from typing import Any

import pytest

from kru_agent.config import Config
from kru_agent.llm import ChatResult, ToolsNotSupported


class FakeLLM:
    """Повертає заздалегідь задані відповіді; записує отримані запити."""

    def __init__(self, script: list[ChatResult | Exception]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, on_token=None, think=None) -> ChatResult:
        self.calls.append({"messages": messages, "tools": tools, "think": think})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if on_token and item.content:
            await on_token("content", item.content)
        return item

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "model": "fake", "model_present": True}

    async def close(self) -> None:
        pass


def call(name: str, **args: Any) -> ChatResult:
    return ChatResult(tool_calls=[{"function": {"name": name, "arguments": args}}])


def text(t: str) -> ChatResult:
    return ChatResult(content=t)


def real(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Історія без службових приміток агента (час, план) — лише те, що писали користувач, модель та інструменти."""
    return [m for m in messages if not str(m.get("content") or "").startswith("[Context note")]


def prompt(call_: dict[str, Any]) -> str:
    """Усе, що побачила модель за один виклик: системний промпт, розмова й примітка з часом/планом у кінці."""
    return "\n".join(str(m.get("content") or "") for m in call_["messages"])


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(workspace=tmp_path / "ws", data_dir=tmp_path / "data", users_workspace=tmp_path / "users",
               embed_url="")  # модель ембедингів у тестах — лише фейкова (test_memory.py)
    c.ensure_dirs()
    return c


__all__ = ["FakeLLM", "call", "text", "ToolsNotSupported"]
