"""Веб: пошук (DuckDuckGo через ddgs або SearXNG) та завантаження сторінок/PDF."""

from __future__ import annotations

import asyncio
import io
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup

from . import Tool, ToolContext, ToolError

USER_AGENT = (
    "Mozilla/5.0 (X11; Fedora; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0 KruAgent/0.1"
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


def _searxng_language(region: str) -> str:
    """Регіон у стилі DuckDuckGo («ua-uk», «us-en», «wt-wt») → мова SearXNG («uk», «en», «all»)."""
    parts = region.lower().split("-")
    return parts[1] if len(parts) == 2 and parts[0] != "wt" and parts[1].isalpha() else "all"


async def _searxng_search(base: str, query: str, max_results: int, kind: str, region: str = "wt-wt") -> list[dict[str, Any]]:
    params = {"q": query, "format": "json", "categories": "news" if kind == "news" else "general",
              "language": _searxng_language(region)}
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
            results = await _searxng_search(ctx.config.searxng_url, query, n, kind, region)
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


def _is_public(ip: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(ip.split("%", 1)[0]).is_global
    except ValueError:
        return False


async def _resolve(host: str, port: int) -> list[str]:
    """IP-адреси хоста (IP — як є, без DNS)."""
    import ipaddress
    import socket

    try:
        return [str(ipaddress.ip_address(host.strip("[]")))]
    except ValueError:
        pass
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _public_only(request: httpx.Request) -> None:
    """Перед кожним запитом (і кожним редіректом): адреса має бути в інтернеті, а не в локальній мережі —
    інакше сторінка з «підказкою» для моделі чи редірект на 127.0.0.1 відкрили б llama-server, HolosTTS,
    SearXNG чи машини NetBird. Дозволити: KRU_ALLOW_PRIVATE_URLS=true."""
    host = request.url.host
    try:
        ips = await _resolve(host, request.url.port or 443)
    except OSError as e:
        raise ToolError(f"Не вдалося знайти адресу {host}: {e}") from e
    if not ips or not all(_is_public(ip) for ip in ips):
        raise ToolError(f"{host} — адреса локальної мережі; туди інструменти з інтернетом не ходять "
                        "(дозволити: KRU_ALLOW_PRIVATE_URLS=true на сервері)")


def _web_client(timeout: float, headers: dict[str, str], allow_private: bool) -> httpx.AsyncClient:
    hooks = {} if allow_private else {"request": [_public_only]}
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers, event_hooks=hooks)


async def fetch_page(url: str, timeout: float = 30, allow_private: bool = False) -> tuple[str, str, str]:
    """Завантажує сторінку чи PDF: (кінцевий URL, заголовок, текст)."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        async with _web_client(timeout, {"User-Agent": USER_AGENT, "Accept-Language": "uk,en;q=0.8"},
                               allow_private) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        raise ToolError(f"Не вдалося завантажити {url}: {e}") from e
    if r.status_code >= 400:
        raise ToolError(f"HTTP {r.status_code} для {url}")

    ctype = r.headers.get("content-type", "").lower()
    title = ""
    if ctype.startswith(("image/", "video/", "audio/")) or ctype in {"application/zip", "application/octet-stream"}:
        raise ToolError(f"Це файл ({ctype.split(';')[0]}), а не сторінка — щоб зберегти його, виклич download_url")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        text = await asyncio.to_thread(_pdf_to_text, r.content)
    elif "html" in ctype or "<html" in r.text[:500].lower():
        title, text = await asyncio.to_thread(_html_to_text, r.text)
    else:
        text = r.text
    return str(r.url), title, text


MAX_DOWNLOAD = 50 * 1024 * 1024
_CTYPE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
              "application/pdf": ".pdf", "image/svg+xml": ".svg", "application/zip": ".zip", "text/csv": ".csv"}


def _download_name(url: str, headers: Any, ctype: str) -> str:
    from email.message import Message
    from pathlib import PurePosixPath
    from urllib.parse import unquote, urlparse

    msg = Message()
    msg["content-disposition"] = headers.get("content-disposition", "")
    name = msg.get_filename() or unquote(PurePosixPath(urlparse(url).path).name)
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip(" .")[:120] or "download"
    if "." not in name and ctype in _CTYPE_EXT:
        name += _CTYPE_EXT[ctype]
    return name


async def download_url(ctx: ToolContext, url: str, path: str = "") -> str:
    """Файл з інтернету (картинка, PDF, архів…) → у робочу теку; наявні файли не перезаписуються."""
    from .files import human_size

    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    try:
        async with _web_client(120, headers, ctx.config.allow_private_urls) as client:
            async with client.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise ToolError(f"HTTP {r.status_code} для {url}")
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                if int(r.headers.get("content-length") or 0) > MAX_DOWNLOAD:
                    raise ToolError("Файл більший за 50 МБ")
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_DOWNLOAD:
                        raise ToolError("Файл більший за 50 МБ")
                final_url, resp_headers = str(r.url), r.headers
    except httpx.HTTPError as e:
        raise ToolError(f"Не вдалося завантажити {url}: {e}") from e
    target = ctx.resolve_path(path) if path else ctx.resolve_path("downloads") / _download_name(final_url, resp_headers, ctype)
    if path and (target.is_dir() or path.endswith(("/", "\\"))):
        target = target / _download_name(final_url, resp_headers, ctype)
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        n += 1
        target = target.with_name(f"{stem}-{n}{suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(target.write_bytes, bytes(data))
    ws = ctx.workspace.resolve()
    shown = target.relative_to(ws).as_posix() if ws in target.resolve().parents else str(target)
    note = ""
    if ctype.startswith("text/html"):
        note = "\nУВАГА: сервер віддав HTML-сторінку, а не файл — перевір посилання (потрібне пряме посилання на файл)."
    return f"Завантажено: {shown} ({human_size(len(data))}, {ctype or 'невідомий тип'}){note}"


async def fetch_url(ctx: ToolContext, url: str, offset: int = 0, max_chars: int = 0) -> str:
    limit = int(max_chars) or ctx.config.max_tool_output_chars
    final_url, title, text = await fetch_page(url, allow_private=ctx.config.allow_private_urls)

    offset = max(0, int(offset))
    chunk = text[offset : offset + limit]
    header = f"URL: {final_url}\n" + (f"Заголовок: {title}\n" if title else "")
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
    Tool(
        name="download_url",
        description=(
            "Завантажує файл з інтернету (картинку, PDF, архів, таблицю…) у робочу теку — типово в downloads/. "
            "Потім його можна показати собі (view_image), передати користувачу (send_file) чи в Telegram "
            "(send_telegram з path). Потрібне пряме посилання на файл. До 50 МБ, наявні файли не перезаписує."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Пряме посилання на файл"},
                "path": {"type": "string", "description": "Куди зберегти (необов'язково), напр. 'images/photo.jpg'"},
            },
            "required": ["url"],
        },
        func=download_url,
    ),
]
