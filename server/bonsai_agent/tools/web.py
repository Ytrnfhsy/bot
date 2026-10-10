"""Веб: пошук (DuckDuckGo через ddgs або SearXNG) та завантаження сторінок/PDF."""

from __future__ import annotations

import asyncio
import io
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from . import Tool, ToolContext, ToolError, truncate

USER_AGENT = (
    "Mozilla/5.0 (X11; Fedora; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0 BonsaiAgent/0.1"
)


def _format_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "Нічого не знайдено. Спробуй переформулювати запит (інші ключові слова, англійською)."
    out = []
    for i, r in enumerate(results, 1):
        date = f" ({r['date']})" if r.get("date") else ""
        out.append(f"{i}. {r.get('title', '').strip()}{date}\n   URL: {r.get('url', '')}\n   {r.get('snippet', '').strip()}")
    return "\n".join(out)


def _ddgs_search(query: str, max_results: int, kind: str, region: str) -> list[dict[str, Any]]:
    try:
        from ddgs import DDGS
    except ImportError:  # старіша назва пакета
        from duckduckgo_search import DDGS  # type: ignore[no-redef]

    with DDGS() as ddgs:
        if kind == "news":
            raw = ddgs.news(query, region=region, max_results=max_results)
            return [
                {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("body"), "date": r.get("date")}
                for r in raw
            ]
        raw = ddgs.text(query, region=region, max_results=max_results)
        return [{"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")} for r in raw]


async def _searxng_search(base: str, query: str, max_results: int, kind: str) -> list[dict[str, Any]]:
    params = {"q": query, "format": "json", "categories": "news" if kind == "news" else "general"}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
        r = await client.get(base.rstrip("/") + "/search", params=params)
        r.raise_for_status()
        data = r.json()
    return [
        {"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content"), "date": x.get("publishedDate")}
        for x in data.get("results", [])[:max_results]
    ]


def _is_no_results(e: Exception) -> bool:
    return "no results" in str(e).lower()


async def _search_once(ctx: ToolContext, query: str, n: int, kind: str, region: str, errors: list[str]) -> list[dict[str, Any]] | None:
    """Результати (можливо порожні) або None, якщо всі пошуковики впали з помилкою."""
    ok = False
    if ctx.config.searxng_url:
        try:
            results = await _searxng_search(ctx.config.searxng_url, query, n, kind)
            if results:
                return results
            ok = True
        except Exception as e:  # noqa: BLE001
            errors.append(f"SearXNG: {e}")
    for attempt in range(2):
        try:
            return await asyncio.to_thread(_ddgs_search, query, n, kind, region)
        except Exception as e:  # noqa: BLE001
            if _is_no_results(e):  # ddgs кидає виняток замість порожнього списку
                return []
            if attempt == 0 and "ratelimit" in type(e).__name__.lower() + str(e).lower():
                await asyncio.sleep(2)
                continue
            errors.append(f"DuckDuckGo: {e}")
            break
    return [] if ok else None


async def web_search(ctx: ToolContext, query: str, max_results: int = 6, kind: str = "text", region: str = "wt-wt") -> str:
    max_results = max(1, min(int(max_results), 15))
    kind = "news" if kind == "news" else "text"
    # якщо вузький пошук порожній — розширюємо: новини → звичайний пошук, регіон → увесь світ
    plan = [(kind, region)]
    if kind == "news":
        plan.append(("text", region))
    if region != "wt-wt":
        plan.append(("text", "wt-wt"))

    errors: list[str] = []
    failed = 0
    for i, (k, r) in enumerate(plan):
        results = await _search_once(ctx, query, max_results, k, r, errors)
        if results is None:
            failed += 1
            continue
        if results:
            note = "" if i == 0 else f"(За kind={kind!r}, region={region!r} нічого не знайдено — показую ширший пошук)\n"
            return note + _format_results(results)
    if failed == len(plan):
        raise ToolError("Пошук недоступний: " + "; ".join(dict.fromkeys(errors)))
    return (
        "Нічого не знайдено за цим запитом (це не помилка пошуку). Спробуй коротший запит, "
        "інші ключові слова або англійською; якщо вже маєш потрібні URL — відкрий їх через fetch_url."
    )


def _html_to_text(html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "header", "aside"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines = []
    for el in root.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "td", "th", "blockquote"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in {"h1", "h2", "h3", "h4"}:
            text = "#" * int(el.name[1]) + " " + text
        elif el.name == "li":
            text = "- " + text
        lines.append(text)
    text = "\n".join(lines) if lines else root.get_text("\n", strip=True)
    # прибираємо дублікати поспіль (вкладені li/td)
    deduped: list[str] = []
    for line in text.splitlines():
        if not deduped or deduped[-1] != line:
            deduped.append(line)
    return title, re.sub(r"\n{3,}", "\n\n", "\n".join(deduped))


def _pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join(f"[стор. {i + 1}]\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages))


async def fetch_url(ctx: ToolContext, url: str, offset: int = 0, max_chars: int = 0) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    limit = int(max_chars) or ctx.config.max_tool_output_chars
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT, "Accept-Language": "uk,en;q=0.8"}
        ) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        raise ToolError(f"Не вдалося завантажити {url}: {e}") from e
    if r.status_code >= 400:
        raise ToolError(f"HTTP {r.status_code} для {url}")

    ctype = r.headers.get("content-type", "").lower()
    title = ""
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        text = await asyncio.to_thread(_pdf_to_text, r.content)
    elif "html" in ctype or "<html" in r.text[:500].lower():
        title, text = _html_to_text(r.text)
    else:
        text = r.text

    offset = max(0, int(offset))
    chunk = text[offset : offset + limit]
    header = f"URL: {r.url}\n" + (f"Заголовок: {title}\n" if title else "")
    header += f"Символи {offset}–{offset + len(chunk)} з {len(text)}"
    if offset + limit < len(text):
        header += f" (є продовження: виклич fetch_url з offset={offset + limit})"
    return f"{header}\n\n{chunk}"


TOOLS = [
    Tool(
        name="web_search",
        description=(
            "Пошук в інтернеті. Повертає список результатів (заголовок, URL, фрагмент). "
            "Для актуальних подій використовуй kind='news'. Після пошуку відкривай найкращі джерела через fetch_url."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Пошуковий запит"},
                "max_results": {"type": "integer", "description": "Кількість результатів (1-15), типово 6"},
                "kind": {"type": "string", "enum": ["text", "news"], "description": "text — звичайний, news — новини"},
                "region": {"type": "string", "description": "Регіон, напр. 'ua-uk', 'us-en', 'wt-wt' (усі)"},
            },
            "required": ["query"],
        },
        func=web_search,
    ),
    Tool(
        name="fetch_url",
        description=(
            "Завантажує веб-сторінку або PDF і повертає читабельний текст. "
            "Для довгих документів використовуй offset, щоб читати далі."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Адреса сторінки"},
                "offset": {"type": "integer", "description": "З якого символу читати (для пагінації)"},
                "max_chars": {"type": "integer", "description": "Максимум символів у відповіді"},
            },
            "required": ["url"],
        },
        func=fetch_url,
    ),
]
