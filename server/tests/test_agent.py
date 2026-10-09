from __future__ import annotations

import json

import httpx
import pytest

from bonsai_agent.agent import Agent, parse_json_tool_call, strip_think
from bonsai_agent.llm import OllamaClient, OpenAICompatClient, ToolsNotSupported
from bonsai_agent.storage import Storage
from bonsai_agent.tools import ToolContext, ToolError

from .conftest import FakeLLM, call, text


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
    hist = agent.storage.load_session("s1")
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
        call("edit_file", path="notes/a.txt", old_text="світ", new_text="Bonsai"),
        call("read_file", path="notes/a.txt"),
        text("готово"),
    ])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "файл", rec.emit, rec.confirm)
    assert (cfg.workspace / "notes/a.txt").read_text() == "привіт\nBonsai\n"
    assert "Bonsai" in llm.calls[3]["messages"][-1]["content"]


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
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "search", "arguments": 'ry": "bonsai"}'}}]}}]},
    ]
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=sse)

    c = OpenAICompatClient("http://x:8080", "bonsai")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "recall", "arguments": {"query": "a"}}}]},
        {"role": "tool", "tool_name": "recall", "content": "нічого"},
    ]
    r = await c.chat(history, tools=[{"type": "function"}])
    assert r.tool_calls == [{"function": {"name": "web_search", "arguments": {"query": "bonsai"}}}]
    assert r.thinking == "думаю"
    msgs = seen["body"]["messages"]
    assert msgs[1]["tool_calls"][0]["id"] == msgs[2]["tool_call_id"]
    assert json.loads(msgs[1]["tool_calls"][0]["function"]["arguments"]) == {"query": "a"}
