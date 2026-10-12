"""Збережені чати: стрічка для показу, назва за замовчуванням, експорт у Markdown."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .agent import strip_think
from .storage import default_title  # noqa: F401 — частина API модуля


def _tool_line(tc: dict[str, Any]) -> str:
    fn = tc.get("function", {})
    args = fn.get("arguments", {})
    if isinstance(args, dict):
        brief = ", ".join(f"{k}={str(v)[:80]}" for k, v in args.items())
    else:
        brief = str(args)[:160]
    return f"🔧 {fn.get('name', '?')}: {brief}"


def transcript(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Історія агента → [{"role": user|assistant|tool, "text"}] для показу в роботі/експорту."""
    out: list[dict[str, str]] = []
    for m in messages:
        role, content = m.get("role"), (m.get("content") or "")
        if role == "user":
            if content.startswith("TOOL RESULT") or content.startswith("[view_image]"):
                continue  # службові повідомлення JSON-протоколу / view_image
            text = content
            if m.get("images"):
                text += "\n" + "\n".join(f"📎 {p}" for p in m["images"])
            out.append({"role": "user", "text": text})
        elif role == "assistant":
            for tc in m.get("tool_calls") or []:
                out.append({"role": "tool", "text": _tool_line(tc)})
            text = strip_think(content)
            if text and not m.get("tool_calls"):
                out.append({"role": "assistant", "text": text})
    return out


def to_markdown(title: str, messages: list[dict[str, Any]], agent_name: str = "Kru", when: float | None = None) -> str:
    date = datetime.fromtimestamp(when) if when else datetime.now()
    lines = [f"# {title}", "", f"*{agent_name} · {date:%Y-%m-%d %H:%M}*", ""]
    for item in transcript(messages):
        if item["role"] == "user":
            lines += ["## 🧑 Ти", "", item["text"], ""]
        elif item["role"] == "assistant":
            lines += [f"## 🤖 {agent_name}", "", item["text"], ""]
        else:
            lines += [f"> {item['text']}", ""]
    return "\n".join(lines).rstrip() + "\n"
