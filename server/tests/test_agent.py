from __future__ import annotations

import json

import httpx
import pytest

from kru_agent.agent import Agent, parse_json_tool_call, strip_think
from kru_agent.llm import OllamaClient, OpenAICompatClient, ToolsNotSupported
from kru_agent.storage import Storage
from kru_agent.tools import ToolContext, ToolError

from .conftest import FakeLLM, call, text, real


class Recorder:
    def __init__(self, approve: bool = True) -> None:
        self.events: list[dict] = []
        self.approve = approve
        self.confirms: list[str] = []

    async def emit(self, ev: dict) -> None:
        self.events.append(ev)

    async def confirm(self, tool: str, summary: str, args: dict) -> bool:
        self.confirms.append(tool)
        return self.approve

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


# ---------------- парсинг ----------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ('```json\n{"tool": "web_search", "args": {"query": "x"}}\n```', ("web_search", {"query": "x"})),
        ('Шукаю... {"tool": "recall", "args": {}} готово', ("recall", {})),
        ('<think>hmm {"tool":"no"}</think>{"name": "fetch_url", "arguments": "{\\"url\\": \\"a.b\\"}"}', ("fetch_url", {"url": "a.b"})),
        ("Просто відповідь без JSON", None),
        ('{"answer": 42}', None),
    ],
)
def test_parse_json_tool_call(raw, expected):
    assert parse_json_tool_call(raw) == expected


def test_strip_think():
    assert strip_think("<think>abc</think>\nВідповідь") == "Відповідь"


# ---------------- сховище / пісочниця ----------------
def test_memory_search(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    s.add_memory("Користувач живе у Львові", "профіль")
    s.add_memory("Улюблена мова програмування — Python")
    found = s.search_memories("де живе користувач")
    assert found and "Львові" in found[0]["content"]
    assert s.delete_memory(found[0]["id"])
    assert all("Львові" not in m["content"] for m in s.search_memories("Львові"))


def test_path_sandbox(cfg, tmp_path):
    ctx = ToolContext(cfg, Storage(cfg.db_path))
    assert ctx.resolve_path("a/b.txt") == (cfg.workspace / "a/b.txt").resolve()
    with pytest.raises(ToolError):
        ctx.resolve_path("../outside.txt")
    with pytest.raises(ToolError):
        ctx.resolve_path("/etc/passwd")


# ---------------- цикл агента ----------------
async def test_native_tool_loop(cfg):
    llm = FakeLLM([call("remember", content="Кота звати Мурчик"), text("Запам'ятав!")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    answer = await agent.run("s1", "Запам'ятай: кота звати Мурчик", rec.emit, rec.confirm)
    assert answer == "Запам'ятав!"
    assert agent.storage.search_memories("Мурчик")
    assert rec.types().count("tool_call") == 1 and rec.types()[-1] == "status"
    # друга відповідь моделі бачила результат інструмента
    last_msgs = llm.calls[1]["messages"]
    assert last_msgs[-1]["role"] == "tool" and "Запам'ятовано" in last_msgs[-1]["content"]
    # історія збережена
    hist = real(agent.storage.load_session("s1"))
    assert hist[0]["role"] == "user" and hist[-1]["content"] == "Запам'ятав!"


async def test_dangerous_tool_denied(cfg):
    llm = FakeLLM([call("run_shell", command="rm -rf /tmp/x"), text("Добре, не буду")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder(approve=False)
    await agent.run("s", "видали", rec.emit, rec.confirm)
    assert rec.confirms == ["run_shell"]
    assert "ВІДХИЛИВ" in llm.calls[1]["messages"][-1]["content"]


async def test_python_tool_runs_after_approval(cfg):
    llm = FakeLLM([call("run_python", code="print(6*7)"), text("42")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder(approve=True)
    await agent.run("s", "порахуй", rec.emit, rec.confirm)
    assert "42" in llm.calls[1]["messages"][-1]["content"]


async def test_file_tools(cfg):
    llm = FakeLLM([
        call("write_file", path="notes/a.txt", content="привіт\nсвіт\n"),
        call("edit_file", path="notes/a.txt", old_text="світ", new_text="Kru"),
        call("read_file", path="notes/a.txt"),
        text("готово"),
    ])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "файл", rec.emit, rec.confirm)
    assert (cfg.workspace / "notes/a.txt").read_text() == "привіт\nKru\n"
    assert "Kru" in llm.calls[3]["messages"][-1]["content"]


async def test_fallback_to_json_mode(cfg):
    llm = FakeLLM([
        ToolsNotSupported("model does not support tools"),
        text('```json\n{"tool": "current_datetime", "args": {}}\n```'),
        text("Зараз ранок"),
    ])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    answer = await agent.run("s", "котра година?", rec.emit, rec.confirm)
    assert answer == "Зараз ранок"
    assert not agent.native_tools
    assert llm.calls[1]["tools"] is None
    assert "TOOLS" in llm.calls[1]["messages"][0]["content"]
    assert llm.calls[2]["messages"][-1]["content"].startswith("TOOL RESULT (current_datetime)")


async def test_unknown_tool_reported(cfg):
    llm = FakeLLM([call("teleport", where="Mars"), text("не можу")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "x", rec.emit, rec.confirm)
    assert "Невідомий інструмент" in llm.calls[1]["messages"][-1]["content"]


async def test_max_steps(cfg):
    cfg.max_steps = 2
    llm = FakeLLM([call("current_datetime"), call("current_datetime")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    answer = await agent.run("s", "x", rec.emit, rec.confirm)
    assert "кроків" in answer


# ---------------- HTTP-клієнти LLM ----------------
async def test_ollama_stream_parsing():
    lines = [
        {"message": {"role": "assistant", "content": "", "thinking": "хм"}, "done": False},
        {"message": {"role": "assistant", "content": "При"}, "done": False},
        {"message": {"role": "assistant", "content": "віт", "tool_calls": [{"function": {"name": "recall", "arguments": {"query": "x"}}}]}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 3},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "m" and body["tools"]
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines))

    c = OllamaClient("http://x", "m")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tokens = []

    async def on_token(kind, t):
        tokens.append((kind, t))

    r = await c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}], on_token=on_token)
    assert r.content == "Привіт" and r.thinking == "хм"
    assert r.tool_calls[0]["function"]["name"] == "recall"
    assert ("thinking", "хм") in tokens


async def test_ollama_tools_not_supported():
    def handler(request):
        return httpx.Response(400, json={"error": "registry.ollama.ai/x does not support tools"})

    c = OllamaClient("http://x", "m")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ToolsNotSupported):
        await c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])


async def test_openai_compat_stream():
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "думаю"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "web_", "arguments": '{"que'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "search", "arguments": 'ry": "kru"}'}}]}}]},
    ]
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=sse)

    c = OpenAICompatClient("http://x:8080", "qwen")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "recall", "arguments": {"query": "a"}}}]},
        {"role": "tool", "tool_name": "recall", "content": "нічого"},
    ]
    r = await c.chat(history, tools=[{"type": "function"}])
    assert r.tool_calls == [{"function": {"name": "web_search", "arguments": {"query": "kru"}}}]
    assert r.thinking == "думаю"
    msgs = seen["body"]["messages"]
    assert msgs[1]["tool_calls"][0]["id"] == msgs[2]["tool_call_id"]
    assert json.loads(msgs[1]["tool_calls"][0]["function"]["arguments"]) == {"query": "a"}
    # без явного KRU_TEMPERATURE діють --temp/--top-p/--top-k самого llama-server
    assert "temperature" not in seen["body"]
    assert "chat_template_kwargs" not in seen["body"]  # без вибору — як налаштовано на сервері моделі
    await c.chat(history, think=False)
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_env_example_is_qwen(monkeypatch):
    import os
    from pathlib import Path

    from kru_agent.config import Config

    for k in list(os.environ):
        if k.startswith("KRU_"):
            monkeypatch.delenv(k)
    cfg = Config.from_env(Path(__file__).parent.parent / ".env.example")
    assert (cfg.backend, cfg.llm_url, cfg.model, cfg.temperature) == ("openai", "http://127.0.0.1:8085", "qwen3.6-35b-a3b", None)
    assert cfg.max_history_chars == 120000
    assert cfg.db_path.name == "kru.sqlite3" and cfg.data_dir.name == "kru"


# ---------------- веб-пошук ----------------
async def test_web_search_no_results_falls_back_to_text(cfg, monkeypatch):
    from kru_agent.tools import web

    calls = []

    def fake(query, n, kind, region):
        calls.append(kind)
        if kind == "news":
            raise Exception("No results found.")
        return [{"title": "NetBird 1.0", "url": "https://netbird.io/blog", "snippet": "реліз"}]

    monkeypatch.setattr(web, "_ddgs_search", fake)
    ctx = ToolContext(cfg, Storage(cfg.db_path))
    out = await web.web_search(ctx, "NetBird", kind="news")
    assert calls == ["news", "text"]
    assert "netbird.io/blog" in out and "ширший пошук" in out


async def test_web_search_empty_is_not_error(cfg, monkeypatch):
    from kru_agent.tools import web

    monkeypatch.setattr(web, "_ddgs_search", lambda *a: (_ for _ in ()).throw(Exception("No results found.")))
    ctx = ToolContext(cfg, Storage(cfg.db_path))
    out = await web.web_search(ctx, "щось дуже рідкісне")
    assert "не помилка" in out


async def test_web_search_real_failure_raises(cfg, monkeypatch):
    from kru_agent.tools import web

    monkeypatch.setattr(web, "_ddgs_search", lambda *a: (_ for _ in ()).throw(ConnectionError("network down")))
    ctx = ToolContext(cfg, Storage(cfg.db_path))
    with pytest.raises(ToolError, match="network down"):
        await web.web_search(ctx, "x")


def test_tool_describe_compact():
    from kru_agent.tools import build_registry

    d = build_registry().get("web_search").describe({"query": "NetBird news", "kind": "news"})
    assert d == "query=NetBird news, kind=news"


async def test_read_docx_and_binary(cfg):
    import zipfile

    from kru_agent.tools.files import read_file

    ctx = ToolContext(cfg, Storage(cfg.db_path))
    doc = cfg.workspace / "uploads" / "звіт.docx"
    doc.parent.mkdir(parents=True)
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>Перший </w:t></w:r><w:r><w:t>абзац</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Другий абзац</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr("word/document.xml", xml)
    out = await read_file(ctx, "uploads/звіт.docx")
    assert "Перший абзац" in out and "Другий абзац" in out
    (cfg.workspace / "pic.png").write_bytes(b"\x89PNG\x00\x00")
    with pytest.raises(ToolError, match="зображення"):
        await read_file(ctx, "pic.png")


async def test_agent_name_in_prompt(cfg):
    cfg.agent_name, cfg.persona = "Kru", "привітний, з гумором"
    llm = FakeLLM([text("Я Kru!")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "хто ти?", rec.emit, rec.confirm)
    system = llm.calls[0]["messages"][0]["content"]
    assert "Your name is Kru" in system and "привітний, з гумором" in system


def test_wants_thinking():
    from kru_agent.agent import wants_thinking

    for simple in ("привіт", "котра година?", "яка погода в Києві", "нагадай завтра о 9 про лікаря",
                   "знайди новини про космос", "Як справи?"):
        assert not wants_thinking(simple), simple
    for heavy in ("Поясни, як працює TCP", "напиши скрипт для бекапу", "порахуй 17*23+5",
                  "склади план поїздки на тиждень", "Порівняй iPhone і Pixel", "подумай уважно", "x" * 500,
                  "```python\nprint(1)\n```"):
        assert wants_thinking(heavy), heavy


async def test_auto_think_after_tool_error(cfg):
    """«Авто»: проста задача — без міркувань, але після помилки інструмента модель міркує, як виправитись."""
    llm = FakeLLM([call("no_such_tool"), call("remember", content="x"), text("Готово")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "запам'ятай x", rec.emit, rec.confirm)
    assert [c["think"] for c in llm.calls] == [False, True, False]


async def test_code_tool_args_stream_live():
    """Код, який модель пише в аргументах write_file, іде клієнту шматками (tool_stream) — робот показує його наживо."""
    chunks = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "write_file", "arguments": '{"path": "a.py", '}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"content": "print(1)\\n"}'}}]}}]},
    ]

    def handler(request):
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=sse)

    c = OpenAICompatClient("http://x:8080", "qwen")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    got = []

    async def on_token(kind, text):
        got.append((kind, json.loads(text) if kind == "tool" else text))

    r = await c.chat([{"role": "user", "content": "q"}], tools=[{"type": "function"}], on_token=on_token)
    assert r.tool_calls[0]["function"]["arguments"] == {"path": "a.py", "content": "print(1)\n"}
    deltas = [g[1] for g in got if g[0] == "tool"]
    assert [d["name"] for d in deltas] == ["write_file", "write_file"]
    assert "".join(d["delta"] for d in deltas) == '{"path": "a.py", "content": "print(1)\\n"}'
