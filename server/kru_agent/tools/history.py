"""Пошук у повній історії поточного чату.

Коли довга розмова не влазить у контекст, її початок стискається в підсумок — точні деталі (числа, терміни,
код, відповіді інструментів) з нього випадають. Сама розмова зберігається повністю, тож агент може знайти
й перечитати потрібне місце.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from . import Tool, ToolContext, ToolError, truncate

NOTE_PREFIX = "[Context note"
SNIPPET = 220


def _render(m: dict[str, Any]) -> tuple[str, str] | None:
    """Повідомлення історії → (хто, текст); службові примітки — None."""
    role, content = m.get("role"), str(m.get("content") or "")
    if role == "user":
        if content.startswith(NOTE_PREFIX):
            return None
        if content.startswith("TOOL RESULT"):
            head, _, body = content.partition("\n")
            return head.removeprefix("TOOL RESULT").strip(" ():") or "інструмент", body
        return "користувач", content
    if role == "assistant":
        calls = [
            f"виклик {tc.get('function', {}).get('name')}"
            f"({json.dumps(tc.get('function', {}).get('arguments'), ensure_ascii=False)[:300]})"
            for tc in m.get("tool_calls") or []
        ]
        return "агент", "\n".join([content, *calls]).strip()
    if role == "tool":
        return f"інструмент {m.get('tool_name') or ''}".strip(), content
    return None


def _messages(ctx: ToolContext) -> list[tuple[int, str, str]]:
    if not ctx.session_id:
        raise ToolError("Немає поточного чату (наприклад, у субагента) — шукати ніде")
    history = ctx.storage.load_session(ctx.session_id, ctx.owner)
    out = []
    for i, m in enumerate(history, 1):
        if (r := _render(m)) and r[1].strip():
            out.append((i, r[0], r[1]))
    return out


def _snippet(text: str, word: str) -> str:
    flat = " ".join(text.split())
    pos = flat.casefold().find(word)
    start = max(0, pos - SNIPPET // 3) if pos >= 0 else 0
    end = min(len(flat), start + SNIPPET)
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


async def search_chat(ctx: ToolContext, query: str, max_results: int = 8) -> str:
    words = [w for w in query.casefold().split() if w]
    if not words:
        raise ToolError("Порожній запит")
    msgs = await asyncio.to_thread(_messages, ctx)
    scored = []
    for i, who, text in msgs:
        low = text.casefold()
        hits = sum(1 for w in words if w in low)
        if hits:
            scored.append((hits, i, who, text))
    if not scored:
        return f"У цьому чаті ({len(msgs)} повідомлень) нічого не знайдено за «{query}». Спробуй інші слова."
    best = max(h for h, *_ in scored)
    scored = [s for s in scored if s[0] == best]  # спершу — де є всі (чи найбільше) слова
    shown = sorted(scored, key=lambda s: s[1])[-max(1, min(int(max_results), 20)):]
    lines = [f"Знайдено {len(scored)} повідомл. (показано {len(shown)}; номер — для read_chat):"]
    for _, i, who, text in shown:
        word = next((w for w in words if w in text.casefold()), words[0])
        lines.append(f"#{i} ({who}): {_snippet(text, word)}")
    return "\n".join(lines)


async def read_chat(ctx: ToolContext, number: int, count: int = 1) -> str:
    msgs = {i: (who, text) for i, who, text in await asyncio.to_thread(_messages, ctx)}
    if not msgs:
        raise ToolError("Чат порожній")
    first = int(number)
    wanted = [i for i in sorted(msgs) if i >= first][: max(1, min(int(count), 10))]
    if not wanted:
        raise ToolError(f"Немає повідомлення #{first}; останнє — #{max(msgs)}")
    parts = [f"#{i} ({msgs[i][0]}):\n{msgs[i][1]}" for i in wanted]
    return truncate("\n\n".join(parts), ctx.config.max_tool_output_chars)


TOOLS = [
    Tool(
        name="search_chat",
        description=(
            "Пошук за словами в ПОВНІЙ історії поточного чату, включно з давніми повідомленнями, які вже стиснуто "
            "в підсумок, і результатами інструментів. Для точних деталей із раніших етапів: як перекладено "
            "термін, яке число чи посилання знайдено, який був код. Повертає уривки з номерами повідомлень."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Слова для пошуку (без урахування регістру)"},
                "max_results": {"type": "integer", "description": "Скільки збігів показати (до 20), типово 8"},
            },
            "required": ["query"],
        },
        func=search_chat,
    ),
    Tool(
        name="read_chat",
        description="Читає повідомлення поточного чату повністю за номером із search_chat (count — кілька поспіль).",
        parameters={
            "type": "object",
            "properties": {
                "number": {"type": "integer", "description": "Номер повідомлення (#N з search_chat)"},
                "count": {"type": "integer", "description": "Скільки повідомлень поспіль (до 10), типово 1"},
            },
            "required": ["number"],
        },
        func=read_chat,
    ),
]
