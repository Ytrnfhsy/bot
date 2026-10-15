from __future__ import annotations

import httpx

from kru_agent.agent import Agent
from kru_agent.chats import transcript
from kru_agent.semantic import Embedder, MemoryIndex, normalize
from kru_agent.users import User

from .conftest import FakeLLM, call, text, prompt, real
from .test_agent import Recorder

# «Смисли» для фейкової моделі ембедингів: слова з однієї групи — про одне й те саме
CONCEPTS = [
    ("відпуст", "відпоч", "море", "канікул"),
    ("кав", "чай", "напо"),
    ("кіт", "кот", "кіш"),
    ("сервер", "fedora"),
    ("тепер", "замість"),
]


class FakeEmbedder:
    enabled = True
    model = "fake-embed"

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self.down = False

    async def embed(self, texts, query=False):
        self.calls.append((list(texts), query))
        if self.down:
            return None
        out = []
        for t in texts:
            low = t.lower()
            vec = [float(sum(low.count(w) for w in group)) for group in CONCEPTS] + [0.05]
            out.append(normalize(vec))
        return out

    async def close(self) -> None:
        pass


def with_fake_embedder(agent: Agent) -> FakeEmbedder:
    fake = FakeEmbedder()
    agent.embedder = fake
    agent.memory = MemoryIndex(agent.storage, fake, agent.config.memory_min_score)
    return fake


# ---------------- пам'ять за змістом ----------------
async def test_recall_finds_by_meaning_not_words(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    with_fake_embedder(agent)
    ctx = agent.make_context()
    for fact in ("Влітку їду на море до Одеси", "П'ю каву без цукру", "Мій кіт Барсик"):
        await agent.registry.call(ctx, "remember", {"content": fact})
    out = await agent.registry.call(ctx, "recall", {"query": "коли в мене відпустка?"})
    assert out.splitlines()[0].endswith("Влітку їду на море до Одеси")
    assert "Барсик" not in out  # нерелевантне не тягнемо


async def test_remember_skips_duplicates_and_points_to_related(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    with_fake_embedder(agent)
    ctx = agent.make_context()
    first = await agent.registry.call(ctx, "remember", {"content": "Люблю каву"})
    dup = await agent.registry.call(ctx, "remember", {"content": "люблю каву"})
    assert first.startswith("Запам'ятовано") and dup.startswith("Це вже є в пам'яті")
    assert len(agent.storage.recent_memories(10)) == 1
    related = await agent.registry.call(ctx, "remember", {"content": "Тепер п'ю і чай, і каву"})
    assert "Схожі записи" in related and "Люблю каву" in related  # модель може забути застаріле


async def test_old_memories_are_indexed_lazily_and_fallback_to_words(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    agent.storage.add_memory("Сервер працює на Fedora")  # збережено, поки ембедингів не було
    fake = with_fake_embedder(agent)
    assert agent.storage.memories_without_embedding("owner", fake.model)
    found = await agent.memory.search("що за система на сервері?", "owner")
    assert [m["content"] for m in found] == ["Сервер працює на Fedora"]
    assert not agent.storage.memories_without_embedding("owner", fake.model)

    fake.down = True  # модель ембедингів лягла — шукаємо за словами
    assert [m["content"] for m in await agent.memory.search("Fedora", "owner")] == ["Сервер працює на Fedora"]


async def test_memories_are_per_user_in_semantic_search(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    with_fake_embedder(agent)
    await agent.registry.call(agent.make_context(User("olena", "user")), "remember", {"content": "Їду на море"})
    assert await agent.memory.search("відпустка", "owner") == []


async def test_relevant_memories_searched_once_per_task(cfg):
    llm = FakeLLM([call("current_datetime"), text("готово")])
    agent = Agent(cfg, llm=llm)
    fake = with_fake_embedder(agent)
    agent.storage.add_memory("Влітку їду на море")
    rec = Recorder()
    await agent.run("s", "спланую відпустку", rec.emit, rec.confirm)
    assert all("Влітку їду на море" in prompt(c) for c in llm.calls)
    assert sum(1 for _, q in fake.calls if q) == 1  # запит ембедиться раз на задачу, а не на кожен крок


async def test_embedder_unreachable_is_quiet_and_returns_none(cfg):
    cfg.embed_url = "http://127.0.0.1:9"
    emb = Embedder(cfg)
    assert await emb.embed(["привіт"]) is None
    assert await emb.embed(["привіт"]) is None  # другий раз навіть не стукає (пауза 60 с)
    await emb.close()


async def test_embedder_parses_openai_response(cfg, monkeypatch):
    cfg.embed_url = "http://embed"
    emb = Embedder(cfg)
    seen = {}

    async def fake_post(url, json):
        seen.update(url=url, json=json)
        data = [{"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 4]}]
        return httpx.Response(200, json={"data": data}, request=httpx.Request("POST", url))

    monkeypatch.setattr(emb._client, "post", fake_post)
    vecs = await emb.embed(["a", "b"], query=True)
    assert vecs == [[0.6, 0.8], [0.0, 1.0]]
    assert seen["url"] == "http://embed/v1/embeddings" and seen["json"]["input"][0].startswith("Instruct:")
    await emb.close()


# ---------------- стискання довгих розмов ----------------
async def test_long_chat_is_summarized_not_cut_blindly(cfg):
    cfg.max_history_chars = 600  # з запасом на службові примітки (час) — у справжньому бюджеті вони непомітні
    llm = FakeLLM([
        text("а" * 300),
        text("- користувач питав про перше\n- відповідь: багато «а»"),  # підсумок
        text("б"),
        text("в"),
    ])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("c", "перше", rec.emit, rec.confirm)
    await agent.run("c", "друге " + "x" * 150, rec.emit, rec.confirm)

    summary_call = llm.calls[1]
    assert summary_call["think"] is False and summary_call["tools"] is None
    assert "USER: перше" in summary_call["messages"][1]["content"]
    main = llm.calls[2]["messages"]
    assert "користувач питав про перше" in main[0]["content"]  # підсумок у системному промпті
    assert real(main[1:])[0]["content"].startswith("друге")
    assert {"type": "status", "state": "summarizing"} in rec.events
    stored = agent.storage.load_session("c")
    second = next(i for i, m in enumerate(stored) if str(m["content"]).startswith("друге"))
    # підсумок охоплює все до другої задачі; її примітка (час) лишається з нею
    assert agent.storage.load_summary("c")[1] == second - 1 and stored[second - 1]["content"].startswith("[Context")

    await agent.run("c", "третє", rec.emit, rec.confirm)  # підсумок не перераховується на кожну задачу
    assert len(llm.calls) == 4 and "користувач питав про перше" in llm.calls[3]["messages"][0]["content"]
    # а сама розмова збережена повністю
    assert [m["text"] for m in transcript(agent.storage.load_chat("c")["messages"]) if m["role"] == "user"][0] == "перше"


async def test_summary_failure_falls_back_to_trimming(cfg):
    cfg.max_history_chars = 200
    llm = FakeLLM([text("а" * 300), RuntimeError("модель зайнята"), text("б")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("c", "перше", rec.emit, rec.confirm)
    assert await agent.run("c", "друге", rec.emit, rec.confirm) == "б"
    assert all(m["content"] != "перше" for m in llm.calls[-1]["messages"])
    assert agent.storage.load_summary("c") == ("", 0)


# ---------------- агент сам запам'ятовує ----------------
def test_prompt_asks_to_remember_without_asking(cfg):
    from kru_agent.agent import SYSTEM_PROMPT

    assert "save it right away with `remember`" in SYSTEM_PROMPT and "secrets" in SYSTEM_PROMPT


def test_remember_is_visible_in_chat_history():
    msgs = [
        {"role": "user", "content": "мені 30"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "remember", "arguments": {"content": "Користувачу 30 років"}}}]},
    ]
    assert transcript(msgs)[1] == {"role": "tool", "text": "🧠 Запам'ятав: Користувачу 30 років"}
