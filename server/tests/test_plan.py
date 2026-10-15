from __future__ import annotations

from kru_agent.agent import Agent
from kru_agent.chats import transcript
from kru_agent.tools.plan import normalize_plan

from .conftest import FakeLLM, call, text, prompt
from .test_agent import Recorder

PLAN = [{"title": "Знайти дані", "status": "in_progress"}, {"title": "Написати звіт", "status": "pending"}]
DONE = [{"title": "Знайти дані", "status": "done"}, {"title": "Написати звіт", "status": "done"}]


async def test_plan_is_shown_saved_and_kept_in_prompt(cfg):
    llm = FakeLLM([call("update_plan", steps=PLAN), call("current_datetime"), call("update_plan", steps=DONE),
                   text("Звіт готовий")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    assert await agent.run("c", "велика задача", rec.emit, rec.confirm) == "Звіт готовий"

    plans = [e["steps"] for e in rec.events if e["type"] == "plan"]
    assert plans == [PLAN, DONE]
    # модель бачить поточний план на кожному кроці
    assert "⏳ 1. Знайти дані" in prompt(llm.calls[1])
    assert "✅ 2. Написати звіт" in prompt(llm.calls[3])
    assert agent.open_chat("c")["plan"] == DONE
    assert "📋 План оновлено" in [m["text"] for m in transcript(agent.storage.load_chat("c")["messages"])]

    # нова задача після завершеного плану починається з чистого аркуша
    llm.script.append(text("ок"))
    rec2 = Recorder()
    await agent.run("c", "дрібниця", rec2.emit, rec2.confirm)
    assert {"type": "plan", "steps": []} in rec2.events and agent.open_chat("c")["plan"] == []
    assert "Current plan" not in prompt(llm.calls[-1])


async def test_unfinished_plan_survives_and_gives_more_steps(cfg):
    cfg.max_steps = 3
    steps = [{"title": f"Крок {i}", "status": "pending"} for i in range(5)]
    llm = FakeLLM([call("update_plan", steps=steps)] + [call("current_datetime")] * 6 + [text("частково")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    # без плану агент зупинився б після 3 кроків; з планом із 5 пунктів — має запас
    assert await agent.run("c", "дуже велика задача", rec.emit, rec.confirm) == "частково"
    assert len(agent.open_chat("c")["plan"]) == 5  # незавершений план лишається — можна сказати «продовжуй»


async def test_delegate_runs_subagent_with_fresh_context(cfg):
    (cfg.workspace / "a.txt").write_text("секрет: 42", encoding="utf-8")
    llm = FakeLLM([
        call("delegate", task="Прочитай a.txt і поверни число", context="файл у робочій теці"),
        call("read_file", path="a.txt"),  # це вже субагент
        text("Число: 42"),  # відповідь субагента
        text("Відповідь: 42"),  # основний агент
    ])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    assert await agent.run("c", "яке число у файлі?", rec.emit, rec.confirm) == "Відповідь: 42"

    sub_first = llm.calls[1]["messages"]
    assert "SUB-AGENT" in sub_first[0]["content"]
    assert [m["role"] for m in sub_first[1:]] == ["user"] and "файл у робочій теці" in sub_first[1]["content"]
    sub_tools = {t["function"]["name"] for t in llm.calls[1]["tools"]}
    assert "delegate" not in sub_tools and "update_plan" not in sub_tools and "read_file" in sub_tools
    # основний агент отримує лише результат, а не сирий вміст файлу
    main_last = llm.calls[3]["messages"]
    assert main_last[-1]["content"].startswith("Результат субагента:\nЧисло: 42")
    assert not any("секрет: 42" in str(m.get("content")) for m in main_last)
    # у стрічці видно дії субагента, але не його «фінальну» відповідь як окреме повідомлення
    sub_calls = [e for e in rec.events if e["type"] == "tool_call" and e.get("sub")]
    assert [e["name"] for e in sub_calls] == ["read_file"]
    assert [e["text"] for e in rec.events if e["type"] == "final"] == ["Відповідь: 42"]


async def test_subagent_dangerous_action_still_asks_user(cfg):
    llm = FakeLLM([
        call("delegate", task="Створи файл x.txt"),
        call("write_file", path="x.txt", content="1"),
        text("не дозволили"),
        text("Не вийшло"),
    ])
    agent = Agent(cfg, llm=llm)
    rec = Recorder(approve=False)
    await agent.run("c", "зроби", rec.emit, rec.confirm)
    assert rec.confirms == ["write_file"] and not (cfg.workspace / "x.txt").exists()


def test_normalize_plan():
    plan = normalize_plan(["Простий рядок", {"title": "  Два   пробіли ", "status": "WEIRD"}, {"status": "done"}])
    assert plan == [{"title": "Простий рядок", "status": "pending"}, {"title": "Два пробіли", "status": "pending"}]


# ---------------- mark_step: позначити пункт без переписування всього плану ----------------
CHAPTERS = [{"title": f"Розділ {i}", "status": "in_progress" if i == 1 else "pending"} for i in range(1, 6)]


async def test_mark_step_marks_done_and_advances(cfg):
    llm = FakeLLM([
        call("update_plan", steps=CHAPTERS),
        call("write_file", path="ch1.txt", content="переклад 1"),
        call("mark_step", step=1),
        call("mark_step", step=[2, 3]),
        text("Перекладено 3 розділи"),
    ])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("c", "переклади книгу по розділах", rec.emit, rec.confirm)
    plans = [e["steps"] for e in rec.events if e["type"] == "plan"]
    assert [s["status"] for s in plans[1]] == ["done", "in_progress", "pending", "pending", "pending"]
    assert [s["status"] for s in plans[2]] == ["done", "done", "done", "in_progress", "pending"]
    assert "Залишилось: 2. Зараз: 4. Розділ 4" in llm.calls[4]["messages"][-1]["content"]
    assert "📋 Виконано пункт плану 1" in [m["text"] for m in transcript(agent.storage.load_chat("c")["messages"])]


async def test_mark_step_errors(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    import pytest

    from kru_agent.tools import ToolError

    with pytest.raises(ToolError, match="недоступний"):
        await agent.registry.call(ctx, "mark_step", {"step": 1})
    plan = list(CHAPTERS)

    async def hook(steps):
        plan[:] = steps

    ctx.plan_hook, ctx.plan_get = hook, lambda: plan
    with pytest.raises(ToolError, match="Немає пункту 9"):
        await agent.registry.call(ctx, "mark_step", {"step": 9})
    out = await agent.registry.call(ctx, "mark_step", {"step": [1, 2, 3, 4, 5]})
    assert "Усі кроки плану виконано" in out


async def test_model_is_reminded_to_update_plan(cfg):
    llm = FakeLLM([call("update_plan", steps=CHAPTERS)] + [call("current_datetime")] * 6 + [text("ok")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("c", "книга", rec.emit, rec.confirm)
    prompts = [prompt(c) for c in llm.calls]
    assert not any("REMINDER" in p for p in prompts[:6])  # перші кроки — без нагадувань
    assert "REMINDER: you have made 5 steps" in prompts[6]
