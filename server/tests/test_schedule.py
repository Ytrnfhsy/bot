from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from kru_agent.agent import Agent
from kru_agent.scheduler import MISSED_GRACE, Scheduler, next_after, parse_when
from kru_agent.server import create_app
from kru_agent.tools import ToolError
from kru_agent.tools import telegram as tg_tool
from kru_agent.telegram import TelegramBot, split_message

from .conftest import FakeLLM, call, text
from .test_agent import Recorder
from .test_server import _until

NOW = datetime(2026, 10, 14, 10, 0)  # середа


# ---------------- час ----------------
def test_parse_when():
    assert parse_when("2026-10-14 18:30", NOW) == datetime(2026, 10, 14, 18, 30)
    assert parse_when("2026-10-14T18:30", NOW) == datetime(2026, 10, 14, 18, 30)
    assert parse_when("18:00", NOW) == datetime(2026, 10, 14, 18, 0)
    assert parse_when("09:00", NOW) == datetime(2026, 10, 15, 9, 0)  # уже минуло — завтра
    assert parse_when("+30m", NOW) == NOW + timedelta(minutes=30)
    assert parse_when("+2h", NOW) == NOW + timedelta(hours=2)
    assert parse_when("1d", NOW) == NOW + timedelta(days=1)
    with pytest.raises(ValueError):
        parse_when("колись", NOW)


def test_next_after():
    friday_9 = datetime(2026, 10, 16, 9, 0).timestamp()
    assert next_after(friday_9, "") is None
    assert datetime.fromtimestamp(next_after(friday_9, "daily", friday_9)) == datetime(2026, 10, 17, 9, 0)
    assert datetime.fromtimestamp(next_after(friday_9, "weekdays", friday_9)) == datetime(2026, 10, 19, 9, 0)  # пн
    assert datetime.fromtimestamp(next_after(friday_9, "weekly", friday_9)) == datetime(2026, 10, 23, 9, 0)
    # сервер лежав кілька днів — наступний запуск у майбутньому, а не серія пропущених
    later = datetime(2026, 10, 20, 12, 0).timestamp()
    assert datetime.fromtimestamp(next_after(friday_9, "daily", later)) == datetime(2026, 10, 21, 9, 0)


# ---------------- інструменти ----------------
async def test_schedule_tools(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    out = await agent.registry.call(ctx, "schedule_task", {"task": "Курс долара в Telegram", "when": "+1h", "repeat": "daily"})
    assert out.startswith("Заплановано #1") and "щодня" in out
    with pytest.raises(ToolError, match="минув"):
        await agent.registry.call(ctx, "schedule_task", {"task": "x", "when": "2020-01-01 10:00"})
    with pytest.raises(ToolError, match="repeat"):
        await agent.registry.call(ctx, "schedule_task", {"task": "x", "when": "+1h", "repeat": "monthly"})
    listed = await agent.registry.call(ctx, "list_scheduled", {})
    assert "#1" in listed and "Курс долара" in listed
    other = agent.make_context(__import__("kru_agent.users", fromlist=["User"]).User("olena", "user"))
    assert await agent.registry.call(other, "cancel_scheduled", {"schedule_id": 1}) == "Активного завдання #1 не знайдено"
    assert await agent.registry.call(ctx, "cancel_scheduled", {"schedule_id": 1}) == "Завдання #1 скасовано"
    assert await agent.registry.call(ctx, "list_scheduled", {}) == "Запланованих завдань немає"


# ---------------- планувальник ----------------
async def test_scheduler_runs_due_tasks_and_reschedules(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    st = agent.storage
    now = time.time()
    once = st.add_schedule("owner", "одноразове", now - 10)
    daily = st.add_schedule("owner", "щоденне", now - 10, "daily")
    future = st.add_schedule("owner", "потім", now + 3600)
    missed = st.add_schedule("owner", "давнє", now - MISSED_GRACE - 100)
    busy_user = st.add_schedule("olena", "зайнята", now - 10)
    ran = []

    async def run(item, chat_id):
        ran.append((item["task"], chat_id))
        return "ok"

    sched = Scheduler(st, run, is_busy=lambda owner: owner == "olena")
    for t in await sched.tick(now):
        await t
    assert sorted(task for task, _ in ran) == ["одноразове", "щоденне"]
    by_id = {i["id"]: i for i in st.list_schedules("owner", include_done=True) + st.list_schedules("olena")}
    assert by_id[once]["enabled"] == 0 and by_id[once]["last_status"] == "виконано"
    assert by_id[once]["last_chat"].startswith(f"sched-{once}-")
    assert by_id[daily]["enabled"] == 1 and by_id[daily]["next_run"] > now
    assert by_id[future]["last_run"] is None
    assert by_id[missed]["enabled"] == 0 and "пропущено" in by_id[missed]["last_status"]
    assert by_id[busy_user]["last_run"] is None  # власник зайнятий — запуститься пізніше


async def test_scheduler_failure_is_recorded(cfg):
    agent = Agent(cfg, llm=FakeLLM([]))
    sid = agent.storage.add_schedule("owner", "впаде", time.time() - 1)

    async def run(item, chat_id):
        raise RuntimeError("модель недоступна")

    for t in await Scheduler(agent.storage, run, lambda o: False).tick():
        await t
    item = agent.storage.list_schedules("owner", include_done=True)[0]
    assert item["id"] == sid and item["last_status"] == "помилка: модель недоступна"


def _app(cfg, script):
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM(script))
    app = create_app(cfg, agent=agent)
    return TestClient(app), app, agent


def test_scheduled_task_runs_in_own_chat_and_notifies(cfg):
    client, app, agent = _app(cfg, [text("Курс: 41.5 грн")])
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        agent.storage.add_schedule("owner", "Дізнайся курс долара", time.time() - 1)
        for t in client.portal.call(app.state.scheduler.tick):
            client.portal.call(lambda t=t: t)
        note, seen = _until(ws, "notify")
        assert note["title"] == "⏰ Дізнайся курс долара" and note["text"] == "Курс: 41.5 грн"
        assert not any(e["type"] in {"token", "final", "status"} for e in seen)  # потік не змішується з поточним чатом
        ws.send_json({"type": "chat_open", "id": note["chat_id"]})
        opened, _ = _until(ws, "chat_opened")
        assert opened["title"] == "⏰ Дізнайся курс долара"
        assert opened["transcript"][0]["text"] == "⏰ Дізнайся курс долара"  # службова інструкція прихована
    assert "Заплановане завдання" in agent.llm.calls[0]["messages"][-1]["content"]


def test_scheduled_task_asks_permission_in_robot(cfg):
    client, app, agent = _app(cfg, [call("write_file", path="r.txt", content="1"), text("Записав")])
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        agent.storage.add_schedule("owner", "Запиши файл", time.time() - 1)
        tasks = client.portal.call(app.state.scheduler.tick)
        req, _ = _until(ws, "confirm_request")
        assert req["scheduled"] == "⏰ Запиши файл"
        ws.send_json({"type": "task", "text": "інше", "session_id": "x"})
        assert "заплановане завдання" in _until(ws, "error")[0]["message"]
        ws.send_json({"type": "confirm_response", "id": req["id"], "approved": True})
        assert _until(ws, "notify")[0]["text"] == "Записав"
        for t in tasks:
            client.portal.call(lambda t=t: t)
    assert (cfg.workspace / "r.txt").read_text() == "1"


# ---------------- Telegram ----------------
def test_split_message():
    parts = split_message("абзац\n\n" * 1000, limit=100)
    assert all(len(p) <= 100 for p in parts) and "".join(parts).count("абзац") == 1000


async def test_send_telegram(cfg, monkeypatch):
    sent = []

    async def fake_call(self, method, **params):
        sent.append((method, params))
        return {}

    monkeypatch.setattr(TelegramBot, "_call", fake_call)
    agent = Agent(cfg, llm=FakeLLM([]))
    ctx = agent.make_context()
    with pytest.raises(ToolError, match="KRU_TELEGRAM_BOT_TOKEN"):
        await agent.registry.call(ctx, "send_telegram", {"text": "привіт"})
    cfg.telegram_bot_token = "123:abc"
    with pytest.raises(ToolError, match="kru telegram link"):
        await agent.registry.call(ctx, "send_telegram", {"text": "привіт"})
    agent.storage.set_telegram_chat("owner", "555")
    assert await agent.registry.call(ctx, "send_telegram", {"text": "Курс 41.5"}) == "Надіслано в Telegram"
    assert sent == [("sendMessage", {"chat_id": "555", "text": "Курс 41.5", "disable_web_page_preview": True,
                                     "parse_mode": "HTML"})]
    # у кожного користувача свій чат
    olena = agent.make_context(__import__("kru_agent.users", fromlist=["User"]).User("olena", "user"))
    assert tg_tool.chat_for(olena) is None


async def test_agent_sends_found_info_to_telegram(cfg, monkeypatch):
    sent = []

    async def fake_call(self, method, **params):
        sent.append(params["text"])
        return {}

    monkeypatch.setattr(TelegramBot, "_call", fake_call)
    cfg.telegram_bot_token = "t"
    llm = FakeLLM([call("send_telegram", text="Знайшов: курс 41.5"), text("Надіслав у Telegram ✅")])
    agent = Agent(cfg, llm=llm)
    agent.storage.set_telegram_chat("owner", "1")
    rec = Recorder()
    assert await agent.run("s", "знайди курс і відправ мені в телеграм", rec.emit, rec.confirm) == "Надіслав у Telegram ✅"
    assert sent == ["Знайшов: курс 41.5"] and rec.confirms == []  # своєму власнику — без підтвердження


async def test_scheduler_waits_for_model_after_reboot(cfg):
    """Після перезавантаження ПК агент стартує раніше за llama-server: завдання не «згорає», а чекає на модель."""
    from kru_agent.llm import LLMUnavailable

    agent = Agent(cfg, llm=FakeLLM([]))
    due = time.time() - 600  # «щодня о 7:00», а сервер увімкнули о 7:10
    sid = agent.storage.add_schedule("owner", "цитата дня", due, "daily")
    state = {"ready": False, "fail": False}
    ran = []

    async def ready():
        return state["ready"]

    async def run(item, chat_id):
        if state["fail"]:
            raise LLMUnavailable("Не вдалося підключитися до LLM")
        ran.append(item["id"])
        return "ok"

    sched = Scheduler(agent.storage, run, lambda o: False, ready=ready)
    assert await sched.tick() == []  # модель ще вантажиться — нічого не запускаємо й не зсуваємо
    item = agent.storage.list_schedules("owner")[0]
    assert item["next_run"] == due and item["enabled"] == 1

    state.update(ready=True, fail=True)  # health уже ok, а модель ще віддає помилку
    for t in await sched.tick():
        await t
    item = agent.storage.list_schedules("owner")[0]
    assert item["next_run"] == due and item["enabled"] == 1 and "повторю" in item["last_status"]
    assert await sched.tick() == []  # повтор — не частіше ніж раз на хвилину

    state["fail"] = False
    for t in await sched.tick(now=time.time() + 61):
        await t
    item = agent.storage.list_schedules("owner")[0]
    assert ran == [sid] and item["last_status"] == "виконано" and item["next_run"] > time.time()


def test_scheduled_task_not_repeated_after_model_dies_midway(cfg):
    """Модель упала після першої дії (тут — нове нагадування): повтор зробив би її вдруге — ні, це помилка."""
    from kru_agent.llm import LLMUnavailable

    client, app, agent = _app(cfg, [call("schedule_task", task="нагадай води", when="+5h"),
                                    LLMUnavailable("connect refused")])
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        sid = agent.storage.add_schedule("owner", "зроби щось", time.time() - 1)
        for t in client.portal.call(app.state.scheduler.tick):
            client.portal.call(lambda t=t: t)
        note, _ = _until(ws, "notify")
        assert note.get("error") and "не повторюю" in note["text"]
    item = next(i for i in agent.storage.list_schedules("owner", include_done=True) if i["id"] == sid)
    assert item["enabled"] == 0 and item["last_status"].startswith("помилка")
    assert [i["task"] for i in agent.storage.list_schedules("owner") if i["id"] != sid] == ["нагадай води"]


async def test_cancel_while_running_stays_cancelled(cfg):
    from kru_agent.llm import LLMUnavailable

    agent = Agent(cfg, llm=FakeLLM([]))
    sid = agent.storage.add_schedule("owner", "щодня", time.time() - 1, "daily")

    async def run(item, chat_id):
        assert agent.storage.cancel_schedule(sid, "owner")  # користувач скасував, поки виконувалось
        raise LLMUnavailable("впало")

    for t in await Scheduler(agent.storage, run, lambda o: False).tick():
        await t
    item = agent.storage.list_schedules("owner", include_done=True)[0]
    assert item["enabled"] == 0 and item["last_status"] == "скасовано"


async def test_explicit_offset_and_hourly_over_dst(cfg):
    from zoneinfo import ZoneInfo

    kyiv = ZoneInfo("Europe/Kyiv")
    at = parse_when("2026-12-01T18:00+02:00", datetime(2026, 11, 30, 12, 0), tz=kyiv)
    assert at == datetime(2026, 12, 1, 18, 0)  # у поясі користувача, а не сервера
    # 25.10.2026 о 04:00 у Києві годинник іде на 03:00: щогодини — кожні 3600 с, без пропущеної години
    ts = datetime(2026, 10, 25, 2, 30, tzinfo=kyiv).timestamp()
    runs = [ts]
    for _ in range(3):
        runs.append(next_after(runs[-1], "hourly", runs[-1], kyiv))
    assert [b - a for a, b in zip(runs, runs[1:])] == [3600, 3600, 3600]
