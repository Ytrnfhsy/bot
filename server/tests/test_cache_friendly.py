"""Контекст, зручний для кешу llama-server: від кроку до кроку змінюється лише кінець запиту.

llama-server пам'ятає вже оброблений початок промпта й перераховує лише те, що після першої відмінності.
Тому системний промпт і вже надіслана розмова мають лишатися байт-у-байт тими самими.
"""

from __future__ import annotations

from kru_agent.agent import Agent

from .conftest import FakeLLM, call, prompt, text
from .test_agent import Recorder
from .test_memory import with_fake_embedder

PLAN = [{"title": "Знайти дані", "status": "in_progress"}, {"title": "Написати звіт", "status": "pending"}]


async def test_prefix_is_stable_between_steps(cfg):
    script = [call("update_plan", steps=PLAN), call("current_datetime"), call("mark_step", step=1),
              call("current_datetime")] + [call("current_datetime")] * 6 + [text("готово")]
    llm = FakeLLM(script)
    agent = Agent(cfg, llm=llm)
    with_fake_embedder(agent)
    agent.storage.add_memory("Звіти користувач любить короткі")
    rec = Recorder()
    await agent.run("c", "зроби звіт", rec.emit, rec.confirm)

    calls = [c["messages"] for c in llm.calls]
    assert len({m[0]["content"] for m in calls}) == 1  # системний промпт не змінюється взагалі
    for prev, cur in zip(calls, calls[1:]):
        # кожен запит ПОВНІСТЮ починається з попереднього — лише дописується в кінець.
        # Для гібридних моделей (Qwen3.5/3.6) це умова, щоб llama-server узяв попередній стан із кешу
        assert cur[:len(prev)] == prev
    # шаблон Qwen3: system — лише першим
    assert all(m["role"] != "system" for c in calls for m in c[1:])
    notes = [m["content"] for m in calls[-1] if str(m.get("content")).startswith("[Context note")]
    first = notes[0]
    assert "Current local time:" in first and "Звіти користувач любить короткі" in first
    assert "любить короткі" not in calls[-1][0]["content"]
    assert calls[0][-1]["content"] == "зроби звіт" and calls[0][-2]["content"] == first  # спершу контекст, потім задача
    assert any("⏳ 1. Знайти дані" in n for n in notes) and any("✅ 1. Знайти дані" in n for n in notes)
    assert any("REMINDER" in n for n in notes)
    # план не повторюється щокроку — лише коли змінився
    assert sum("Current plan" in n for n in notes) == 2
    # у стрічці чату примітки не видно
    assert all("Context note" not in item["text"] for item in agent.open_chat("c")["transcript"])


async def test_trimming_jumps_instead_of_sliding(cfg, monkeypatch):
    cfg.summarize = False
    cfg.max_history_chars = 6000
    llm = FakeLLM([call("run_python", code="print('x' * 400)")] * 30 + [text("ok")])
    agent = Agent(cfg, llm=llm)
    cfg.auto_approve = True

    async def fake_python(ctx, code, timeout=0):
        return "y" * 400

    monkeypatch.setattr(agent.registry.get("run_python"), "func", fake_python)
    rec = Recorder()
    await agent.run("c", "довга задача", rec.emit, rec.confirm)
    calls = [[m for m in c["messages"] if not str(m.get("content")).startswith("[Context note")] for c in llm.calls]
    # задача користувача лишається на місці завжди, навіть коли старі кроки відкинуто
    assert all(c[1]["role"] == "user" and c[1]["content"].startswith("довга задача") for c in calls)
    # початок розмови після задачі змінюється лише зрідка (стрибками), а не на кожному кроці
    jumps = sum(1 for a, b in zip(calls, calls[1:]) if len(b) < len(a))  # контекст скоротився — початок змінився
    grows = sum(1 for a, b in zip(calls, calls[1:]) if len(b) > len(a) and b[:len(a) - 1] == a[:-1])
    assert 0 < jumps <= len(calls) // 4 and grows >= len(calls) // 2  # здебільшого — лише дописується в кінець
    assert all(c[2]["role"] == "assistant" for c in calls[1:])  # пари виклик/результат не розірвано
    assert all(sum(len(str(m.get("content"))) for m in c["messages"][1:]) < 6000 + 2000 for c in llm.calls)


async def test_switch_off_restores_old_prompt(cfg):
    cfg.cache_friendly = False
    llm = FakeLLM([call("update_plan", steps=PLAN), text("ok")])
    agent = Agent(cfg, llm=llm)
    await agent.run("c", "звіт", Recorder().emit, Recorder().confirm)
    system = llm.calls[1]["messages"][0]["content"]
    assert "Current plan" in system and "Context note" not in prompt(llm.calls[1])


async def test_subagent_gets_its_own_static_prompt(cfg):
    llm = FakeLLM([call("delegate", task="порахуй"), text("4"), text("Відповідь 4")])
    agent = Agent(cfg, llm=llm)
    await agent.run("c", "делегуй", Recorder().emit, Recorder().confirm)
    sub = llm.calls[1]["messages"]
    assert "SUB-AGENT" in sub[0]["content"] and "Context note" not in prompt(llm.calls[1])
