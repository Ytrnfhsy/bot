"""Збережені чати: стрічка для показу, назва за замовчуванням, експорт у Markdown."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from .agent import parse_json_tool_call, strip_think
from .storage import default_title  # noqa: F401 — частина API модуля


def _tool_line(tc: dict[str, Any]) -> str:
    fn = tc.get("function", {})
    args = fn.get("arguments", {})
    if fn.get("name") == "update_plan":
        return "📋 План оновлено"
    if fn.get("name") == "mark_step" and isinstance(args, dict):
        return f"📋 Виконано пункт плану {args.get('step')}"
    if fn.get("name") == "delegate" and isinstance(args, dict):
        return f"🤝 Доручено субагенту: {str(args.get('task', ''))[:200]}"
    if fn.get("name") == "remember" and isinstance(args, dict):
        return f"🧠 Запам'ятав: {str(args.get('content', ''))[:200]}"
    if isinstance(args, dict):
        brief = ", ".join(f"{k}={str(v)[:80]}" for k, v in args.items())
    else:
        brief = str(args)[:160]
    return f"🔧 {fn.get('name', '?')}: {brief}"


SENT_FILE = "Користувач отримав кнопку завантаження"  # початок результату send_file, коли файл передано


def _arg_path(args: Any) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return ""
    return str(args.get("path") or "") if isinstance(args, dict) else ""


def _sent_file(pending: list[str], content: str) -> dict[str, Any] | None:
    """Результат send_file → рядок стрічки з кнопкою завантаження (якщо файл справді передано)."""
    path = pending.pop(0) if pending else ""
    if not path or not content.startswith(SENT_FILE):
        return None
    name = PurePosixPath(path.replace("\\", "/")).name
    return {"role": "tool", "text": f"📎 Файл: {name}", "file": {"path": path, "name": name}}


def transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Історія агента → [{"role": user|assistant|tool, "text"}] для показу в роботі/експорту.
    Передані файли (send_file) — рядки "tool" з полем file: {path, name} (у панелі це кнопка завантаження)."""
    out: list[dict[str, Any]] = []
    files: list[str] = []  # шляхи з викликів send_file, що чекають на свій результат
    for m in messages:
        role, content = m.get("role"), (m.get("content") or "")
        if role == "tool" and m.get("tool_name") == "send_file":
            if item := _sent_file(files, content):
                out.append(item)
        elif role == "user":
            if content.startswith("TOOL RESULT (send_file)"):
                if item := _sent_file(files, content.split("\n", 1)[-1]):
                    out.append(item)
                continue
            if content.startswith(("TOOL RESULT", "[view_image]", "[Context note")):
                continue  # службові повідомлення JSON-протоколу / view_image / примітки агента (час, план)
            text = content
            if content.startswith("⏰ Заплановане завдання") and "\n" in content:
                text = "⏰ " + content.split("\n", 1)[1]  # службову інструкцію не показуємо
            if m.get("images"):
                text += "\n" + "\n".join(f"📎 {p}" for p in m["images"])
            out.append({"role": "user", "text": text})
        elif role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                if fn.get("name") == "send_file":
                    files.append(_arg_path(fn.get("arguments")))
                    continue  # рядок із кнопкою — коли прийде результат
                out.append({"role": "tool", "text": _tool_line(tc)})
            if not m.get("tool_calls") and (call := parse_json_tool_call(content)) and call[0] == "send_file":
                files.append(_arg_path(call[1]))  # JSON-режим: виклик записано текстом
                continue
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


SNIPPET = 140


def _snippet(text: str, word: str) -> str:
    """Уривок навколо першого збігу (одним рядком)."""
    flat = " ".join(text.split())
    pos = flat.casefold().find(word)
    if pos < 0:
        return flat[:SNIPPET] + ("…" if len(flat) > SNIPPET else "")
    start = max(0, pos - SNIPPET // 3)
    end = min(len(flat), start + SNIPPET)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def search(chats: list[dict[str, Any]], query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Пошук чатів за назвою й текстом розмови (усі слова запиту, без урахування регістру).
    Збіги в назві — вище; далі — новіші. До кожного результату — уривок зі збігом."""
    words = [w for w in query.casefold().split() if w]
    if not words:
        return []
    found: list[tuple[int, float, dict[str, Any]]] = []
    for chat in chats:
        title = chat["title"] or ""
        messages = json.loads(chat["messages"]) if isinstance(chat["messages"], str) else chat["messages"]
        texts = [item["text"] for item in transcript(messages) if item["role"] in {"user", "assistant"}]
        haystack = (title + "\n" + "\n".join(texts)).casefold()
        if not all(w in haystack for w in words):
            continue
        in_title = all(w in title.casefold() for w in words)
        hit = next((t for t in texts if words[0] in t.casefold()), "") or next(
            (t for t in texts if any(w in t.casefold() for w in words)), "")
        word = words[0] if words[0] in hit.casefold() else next((w for w in words if w in hit.casefold()), "")
        info = {k: chat[k] for k in ("id", "title", "count", "created_at", "updated_at")}
        info["snippet"] = _snippet(hit, word) if hit else ""
        found.append((0 if in_title else 1, -chat["updated_at"], info))
    found.sort(key=lambda x: (x[0], x[1]))
    return [info for *_, info in found[:limit]]
