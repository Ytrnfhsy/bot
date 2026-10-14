"""Глибоке дослідження: кілька пошукових запитів → найкращі сторінки → нотатки з кожної → джерела.

Звіт пише сам агент (на основі нотаток), тож відповідь стрімиться користувачу як звичайно.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import Tool, ToolContext, ToolError
from .web import _search_once, fetch_page

DEPTHS = {
    # запитів, сторінок, додатковий раунд пошуку прогалин
    "quick": (3, 4, False),
    "normal": (4, 6, False),
    "deep": (5, 8, True),
}
PAGE_CHARS = 9000  # скільки тексту сторінки дати моделі для нотаток
SKIP_HOSTS = ("youtube.com", "youtu.be", "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com")

QUERIES_PROMPT = """You plan web research. Question: {question}
Today: {today}. Write {n} diverse web search queries that together cover the question: different angles,
synonyms, at least one in English and one in the question's language, add a year for time-sensitive topics.
Reply with ONLY a JSON array of strings."""

FOLLOWUP_PROMPT = """Question: {question}
Notes collected so far:
{notes}

What important aspects are still missing or contradictory? Reply with ONLY a JSON array of up to {n} new web
search queries to fill the gaps, or [] if the notes are already sufficient."""

NOTES_PROMPT = """Research question: {question}
Below is the text of a web page ({url}). Extract ONLY information relevant to the question as concise bullet
points in {language}: facts, numbers, dates, names, quotes, opinions (mark them as opinions). Keep exact figures.
No introductions. If the page has nothing relevant, reply exactly: NONE

PAGE TEXT:
{text}"""


def _canonical(url: str) -> str:
    parts = urlsplit(url)
    host = parts.netloc.lower().removeprefix("www.")
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), parts.query, ""))


def _json_list(text: str) -> list[str]:
    from ..agent import strip_think

    text = strip_think(text)
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    return [str(x).strip() for x in data if isinstance(x, (str, int, float)) and str(x).strip()]


async def _ask(ctx: ToolContext, prompt: str) -> str:
    from ..agent import strip_think

    result = await ctx.llm.chat([{"role": "user", "content": prompt}], think=False)
    return strip_think(result.content or "")


def _rank(batches: list[list[dict[str, Any]]], seen: set[str]) -> list[dict[str, Any]]:
    """Сторінки з усіх запитів: вище ті, що трапились у кількох запитах і стоять вище у видачі."""
    score: dict[str, float] = {}
    first: dict[str, dict[str, Any]] = {}
    for results in batches:
        for rank, r in enumerate(results):
            url = r.get("url") or ""
            if not url.startswith(("http://", "https://")) or any(h in urlsplit(url).netloc for h in SKIP_HOSTS):
                continue
            key = _canonical(url)
            if key in seen:
                continue
            score[key] = score.get(key, 0.0) + 1.0 / (rank + 2)
            first.setdefault(key, r)
    return [first[k] for k in sorted(score, key=lambda k: score[k], reverse=True)]


async def _search_all(ctx: ToolContext, queries: list[str]) -> list[list[dict[str, Any]]]:
    errors: list[str] = []
    results = await asyncio.gather(*(_search_once(ctx, q, 8, "text", "wt-wt", errors) for q in queries))
    batches = [r for r in results if r]
    if not batches and errors:
        raise ToolError("Пошук недоступний: " + "; ".join(dict.fromkeys(errors)))
    return batches


async def _read_pages(
    ctx: ToolContext, question: str, candidates: list[dict[str, Any]], want: int, sources: list[dict[str, Any]],
    seen: set[str],
) -> int:
    """Відкриває сторінки паралельно (із запасом на ті, що не відкриються) і по черзі робить нотатки."""
    batch = candidates[: want + 3]
    for r in batch:
        seen.add(_canonical(r["url"]))
    await ctx.report(f"відкриваю {len(batch)} сторінок…")
    pages = await asyncio.gather(*(fetch_page(r["url"], timeout=20) for r in batch), return_exceptions=True)
    failed, used = 0, 0
    for r, page in zip(batch, pages):
        if used >= want:
            break
        if isinstance(page, BaseException) or len(page[2].strip()) < 200:
            failed += 1
            continue
        url, title, text = page
        title = title or r.get("title") or url
        await ctx.report(f"читаю джерело {len(sources) + 1}: {title[:60]}")
        notes = await _ask(ctx, NOTES_PROMPT.format(
            question=question, url=url, language=ctx.config.language, text=text[:PAGE_CHARS]))
        used += 1
        if notes and notes.strip().upper() != "NONE":
            sources.append({"url": url, "title": title, "notes": notes.strip()})
    return failed


async def deep_research(ctx: ToolContext, question: str, depth: str = "normal") -> str:
    if ctx.llm is None:
        raise ToolError("Дослідження недоступне: немає доступу до моделі")
    if not question.strip():
        raise ToolError("Порожнє питання")
    n_queries, n_pages, followup = DEPTHS.get(depth, DEPTHS["normal"])
    from datetime import date

    await ctx.report("складаю пошукові запити…")
    queries = _json_list(await _ask(ctx, QUERIES_PROMPT.format(question=question, n=n_queries, today=date.today())))
    queries = list(dict.fromkeys([question, *queries]))[: n_queries + 1]

    await ctx.report(f"шукаю за {len(queries)} запитами…")
    seen: set[str] = set()
    sources: list[dict[str, Any]] = []
    failed = await _read_pages(ctx, question, _rank(await _search_all(ctx, queries), seen), n_pages, sources, seen)

    if followup and sources:
        await ctx.report("шукаю, чого бракує…")
        notes = "\n".join(f"[{i}] {s['notes'][:600]}" for i, s in enumerate(sources, 1))
        extra = _json_list(await _ask(ctx, FOLLOWUP_PROMPT.format(question=question, notes=notes, n=3)))
        extra = [q for q in extra if q not in queries][:3]
        if extra:
            queries += extra
            await ctx.report(f"додатковий пошук: {len(extra)} запити…")
            failed += await _read_pages(ctx, question, _rank(await _search_all(ctx, extra), seen), 4, sources, seen)

    if not sources:
        return (
            "Дослідження не дало результатів: релевантних сторінок не знайдено або вони не відкрились "
            f"(запити: {'; '.join(queries)}). Спробуй web_search з іншими словами."
        )
    out = [
        f"ДОСЛІДЖЕННЯ: {question}",
        f"Запити: {'; '.join(queries)}",
        f"Опрацьовано джерел: {len(sources)}" + (f" (не відкрилось: {failed})" if failed else ""),
        "",
    ]
    for i, s in enumerate(sources, 1):
        out += [f"[{i}] {s['title']} — {s['url']}", s["notes"], ""]
    out.append(
        "Тепер напиши відповідь-звіт на основі цих нотаток: структуровано, з посиланнями на джерела у вигляді [1], [2] "
        "і списком джерел (URL) наприкінці. Якщо джерела суперечать одне одному — скажи про це."
    )
    return "\n".join(out)


TOOLS = [
    Tool(
        name="deep_research",
        description=(
            "Глибоке дослідження теми в інтернеті: сам складає кілька запитів, відкриває найкращі сторінки й "
            "повертає нотатки з нумерованими джерелами. Для складних питань, порівнянь, оглядів, звітів, "
            "«розберись у темі». Триває 1–5 хвилин. Для простого факту достатньо web_search."
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Що саме дослідити (повне питання)"},
                "depth": {
                    "type": "string",
                    "enum": ["quick", "normal", "deep"],
                    "description": "quick — 4 джерела, normal — 6, deep — 8 + додатковий пошук прогалин",
                },
            },
            "required": ["question"],
        },
        func=deep_research,
        summarize=lambda a: f"{a.get('question', '')} ({a.get('depth', 'normal')})",
    ),
]
