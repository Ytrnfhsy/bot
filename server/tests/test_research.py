from __future__ import annotations

import httpx
import pytest

from kru_agent.agent import Agent
from kru_agent.tools import ToolError, web
from kru_agent.tools import research

from .conftest import FakeLLM, call, text
from .test_agent import Recorder

PAGES = {
    "https://a.com/x": ("Сторінка A", "Факти A. " * 50),
    "https://b.com/y": ("Сторінка B", "Факти B. " * 50),
    "https://c.com/z": ("Сторінка C", "Факти C. " * 50),
    "https://short.com/": ("Порожня", "мало"),
}


@pytest.fixture
def fake_web(monkeypatch):
    searches: list[str] = []
    results = {
        "питання": [{"title": "A", "url": "https://a.com/x"}, {"title": "S", "url": "https://short.com/"}],
        "query en": [{"title": "B", "url": "https://www.b.com/y/"}, {"title": "A", "url": "https://a.com/x#top"}],
        "запит 2": [{"title": "YT", "url": "https://youtube.com/watch?v=1"}, {"title": "Down", "url": "https://down.com/"}],
        "прогалина": [{"title": "C", "url": "https://c.com/z"}, {"title": "A", "url": "https://a.com/x"}],
    }

    async def fake_search(ctx, query, n, kind, region, errors):
        searches.append(query)
        return results.get(query, [])

    async def fake_fetch(url, timeout=30):
        for key, (title, body) in PAGES.items():
            if research._canonical(key) == research._canonical(url):
                return key, title, body
        raise ToolError(f"HTTP 500 для {url}")

    monkeypatch.setattr(research, "_search_once", fake_search)
    monkeypatch.setattr(research, "fetch_page", fake_fetch)
    return searches


async def test_deep_research_collects_notes_with_sources(cfg, fake_web):
    llm = FakeLLM([
        text('Ось запити:\n```json\n["query en", "запит 2"]\n```'),
        text("- A каже 42"),  # нотатки з a.com (найвище: є у двох запитах)
        text("NONE"),  # b.com — нічого корисного
        text("Остаточний звіт [1]"),
    ])
    agent = Agent(cfg, llm=llm)
    llm.script.insert(0, call("deep_research", question="питання", depth="quick"))
    rec = Recorder()
    answer = await agent.run("s", "досліди питання", rec.emit, rec.confirm)

    assert answer == "Остаточний звіт [1]"
    assert fake_web == ["питання", "query en", "запит 2"]
    internal = llm.calls[1:4]
    assert all(c["think"] is False and c["tools"] is None for c in internal)
    assert "https://a.com/x" in internal[1]["messages"][0]["content"]
    report = llm.calls[-1]["messages"][-1]["content"]
    assert "[1] Сторінка A — https://a.com/x" in report and "A каже 42" in report
    assert "Сторінка B" not in report  # NONE не потрапляє в джерела
    assert "youtube" not in report and "не відкрилось: 2" in report  # коротка сторінка і down.com
    progress = [e["text"] for e in rec.events if e["type"] == "progress"]
    assert progress[0] == "складаю пошукові запити…" and any("читаю джерело" in p for p in progress)


async def test_deep_mode_searches_for_gaps(cfg, fake_web):
    llm = FakeLLM([
        text('["query en"]'),
        text("- A"), text("- B"),
        text('["прогалина"]'),
        text("- C"),
    ])
    ctx = Agent(cfg, llm=llm).make_context()
    out = await research.deep_research(ctx, "питання", depth="deep")
    assert fake_web[-1] == "прогалина"
    assert "[3] Сторінка C — https://c.com/z" in out and out.count("https://a.com/x") == 1


async def test_research_bad_query_json_still_searches_question(cfg, fake_web):
    llm = FakeLLM([text("не можу скласти запити"), text("NONE")])
    out = await research.deep_research(Agent(cfg, llm=llm).make_context(), "питання", depth="quick")
    assert fake_web == ["питання"] and "не дало результатів" in out


async def test_research_search_down_is_error(cfg, monkeypatch):
    async def broken(ctx, query, n, kind, region, errors):
        errors.append("DuckDuckGo: network down")
        return None

    monkeypatch.setattr(research, "_search_once", broken)
    ctx = Agent(cfg, llm=FakeLLM([text("[]")])).make_context()
    with pytest.raises(ToolError, match="Пошук недоступний"):
        await research.deep_research(ctx, "x")


def test_research_allowed_for_regular_users():
    from kru_agent.users import ADMIN_ONLY_TOOLS

    assert "deep_research" not in ADMIN_ONLY_TOOLS


async def test_searxng_gets_language_from_region(monkeypatch):
    seen = {}

    async def fake_get(self, url, params=None):
        seen.update(url=url, params=params)
        return httpx.Response(200, json={"results": [{"title": "t", "url": "https://u", "content": "c"}]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    out = await web._searxng_search("http://127.0.0.1:8888/", "київ", 5, "text", "ua-uk")
    assert seen["url"] == "http://127.0.0.1:8888/search"
    assert seen["params"]["language"] == "uk" and seen["params"]["format"] == "json"
    assert out[0]["url"] == "https://u"
    assert web._searxng_language("wt-wt") == "all"
