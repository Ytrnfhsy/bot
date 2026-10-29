from __future__ import annotations

import io
import zipfile

import pytest

from kru_agent import skills as sk
from kru_agent.agent import Agent
from kru_agent.llm import ChatResult
from kru_agent.tools import ToolError

from .conftest import FakeLLM, prompt, text


def test_builtin_skill_parses():
    found = sk.all_skills(None)
    nist = found["nist-csp-exam"]
    assert nist.builtin and "NIST Cybersecurity Professional" in nist.description
    assert "Govern (GV)" in nist.body and not nist.body.startswith("---")


async def test_skill_listed_in_prompt_and_loaded_by_tool(cfg):
    use = ChatResult(tool_calls=[{"function": {"name": "use_skill", "arguments": {"name": "nist-csp-exam"}}}])
    agent = Agent(cfg, llm=FakeLLM([use, text("Почнімо!")]))
    await agent.run("s", "підготуй мене до іспиту NIST", lambda e: _noop(e), _allow)
    first = prompt(agent.llm.calls[0])
    assert "- nist-csp-exam: Підготовка до іспиту NIST" in first and "use_skill(name)" in first
    assert "Опорні знання" not in first  # у промпті лише опис, повний текст — через use_skill
    assert "Опорні знання" in str(agent.llm.calls[1]["messages"])


async def test_save_skill_needs_approval_and_overrides(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    out = await agent.registry.call(ctx, "save_skill", {
        "name": "weekly-report", "description": "Тижневий звіт: що зроблено, у Telegram",
        "instructions": "1. Подивись чати за тиждень\n2. Надішли список"})
    assert "збережено" in out
    s = sk.all_skills(ctx.workspace)["weekly-report"]
    assert not s.builtin and s.body.startswith("1. Подивись") and s.description == "Тижневий звіт: що зроблено, у Telegram"
    assert agent.registry.get("save_skill").dangerous  # запис у системний промпт — лише з дозволу
    with pytest.raises(ToolError, match="латиницею"):
        await agent.registry.call(ctx, "save_skill", {"name": "../x", "description": "d", "instructions": "i"})
    # своя з тією ж назвою замінює вбудовану
    await agent.registry.call(ctx, "save_skill", {"name": "nist-csp-exam", "description": "моя", "instructions": "свій план"})
    assert "свій план" in await agent.registry.call(ctx, "use_skill", {"name": "nist-csp-exam"})


async def test_use_skill_files_stay_inside(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    folder = ctx.workspace / "skills" / "server-check"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: server-check\ndescription: перевірка\n---\nЗапусти check.sh", "utf-8")
    (folder / "check.sh").write_text("df -h", "utf-8")
    (ctx.workspace / "secret.txt").write_text("x", "utf-8")
    out = await agent.registry.call(ctx, "use_skill", {"name": "server-check"})
    assert "check.sh" in out
    assert await agent.registry.call(ctx, "use_skill", {"name": "server-check", "file": "check.sh"}) == "df -h"
    with pytest.raises(ToolError):
        await agent.registry.call(ctx, "use_skill", {"name": "server-check", "file": "../../secret.txt"})
    with pytest.raises(ToolError, match="Є:"):
        await agent.registry.call(ctx, "use_skill", {"name": "nope"})


def _pptx(slides: list[tuple[str, str]]) -> bytes:
    ns = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, (body, notes) in enumerate(slides, 1):
            paras = "".join(f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in body.split("\n"))
            z.writestr(f"ppt/slides/slide{i}.xml", f"<p:sld xmlns:p='p' {ns}>{paras}</p:sld>")
            if notes:
                z.writestr(f"ppt/notesSlides/notesSlide{i}.xml",
                           f"<p:notes xmlns:p='p' {ns}><a:p><a:r><a:t>{notes}</a:t></a:r></a:p>"
                           f"<a:p><a:r><a:t>{i}</a:t></a:r></a:p></p:notes>")
    return buf.getvalue()


async def test_read_pptx_with_notes(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    slides = [(f"Тема {i}", "") for i in range(1, 12)]
    slides[1] = ("CSF 2.0 Functions\nGovern", "Govern — нова функція, важливо для іспиту")
    (ctx.workspace / "uploads").mkdir()
    (ctx.workspace / "uploads" / "NCSP.pptx").write_bytes(_pptx(slides))
    out = await agent.registry.call(ctx, "read_file", {"path": "uploads/NCSP.pptx"})
    assert "=== Слайд 2 ===" in out and "Нотатки доповідача: Govern — нова функція" in out
    assert out.index("=== Слайд 9 ===") < out.index("=== Слайд 10 ===")  # 10 після 9, а не після 1


async def _noop(_):
    pass


async def _allow(*_):
    return True


def test_all_builtin_skills_load():
    found = sk.all_skills(None)
    for name in ("nist-csp-exam", "flashcards", "explain-like-mentor", "kru-dev", "job-search", "dork-search"):
        assert name in found and found[name].builtin and found[name].description, name


async def test_flashcards_spaced_repetition(cfg, monkeypatch):
    from datetime import datetime, timedelta

    from kru_agent.tools import flashcards as fc

    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    call_ = agent.registry.call
    out = await call_(ctx, "flashcards_add", {"deck": "nist-csp", "cards": [
        {"front": "6 функцій CSF 2.0?", "back": "Govern, Identify, Protect, Detect, Respond, Recover"},
        {"front": "Tier 2?", "back": "Risk Informed"},
        {"front": "tier 2?", "back": "дубль"},  # той самий текст — не додається
        {"front": "", "back": "порожнє"}]})
    assert "Додано 2" in out
    due = await call_(ctx, "flashcards_due", {})
    assert "До повторення: 2" in due and "Risk Informed" in due
    await call_(ctx, "flashcards_grade", {"deck": "nist-csp", "card_id": 1, "correct": True})
    out = await call_(ctx, "flashcards_grade", {"deck": "nist-csp", "card_id": 2, "correct": False})
    assert "коробка 0" in out
    due = await call_(ctx, "flashcards_due", {"deck": "nist-csp"})
    assert "До повторення: 1" in due and "#2" in due  # помилкова — ще сьогодні, правильна — завтра

    class Later(datetime):  # через 2 дні: обидві знову на повторенні
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=2)

    monkeypatch.setattr(fc, "datetime", Later)
    assert "До повторення: 2" in await call_(ctx, "flashcards_due", {})
    stats = await call_(ctx, "flashcards_stats", {})
    assert "«nist-csp»: 2 карток" in stats and "найважчі: Tier 2?" in stats
    with pytest.raises(ToolError, match="Назва колоди"):
        await call_(ctx, "flashcards_add", {"deck": "../x", "cards": [{"front": "a", "back": "b"}]})
