"""Часовий пояс користувача: агент бачить і планує час у ньому, а не в UTC сервера."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from kru_agent.agent import Agent
from kru_agent.scheduler import describe, next_after

from .conftest import FakeLLM, call, text
from .test_agent import Recorder
from .test_server import _client, _until

KYIV = ZoneInfo("Europe/Kyiv")


async def test_prompt_shows_user_local_time_and_tz_is_remembered(cfg):
    llm = FakeLLM([text("ок"), text("ок")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "котра година?", rec.emit, rec.confirm, tz="Europe/Kyiv")
    prompt = llm.calls[0]["messages"][0]["content"]
    assert "timezone Europe/Kyiv, UTC+0" in prompt
    assert datetime.now(KYIV).strftime("%Y-%m-%d %H:") in prompt
    assert "never convert to UTC" in prompt
    # наступна задача без tz (напр. із CLI) — береться запам'ятований пояс користувача
    await agent.run("s", "а зараз?", rec.emit, rec.confirm)
    assert "Europe/Kyiv" in llm.calls[1]["messages"][0]["content"]


async def test_bad_tz_falls_back_to_config(cfg):
    cfg.timezone = "America/New_York"
    llm = FakeLLM([text("ок")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "привіт", rec.emit, rec.confirm, tz="Not/AZone")
    assert "America/New_York" in llm.calls[0]["messages"][0]["content"]
    assert agent.storage.user_tz("owner") == ""


async def test_schedule_uses_user_local_time(cfg):
    target = (datetime.now(KYIV) + timedelta(hours=3)).replace(second=0, microsecond=0)
    when = target.strftime("%Y-%m-%d %H:%M")
    llm = FakeLLM([call("schedule_task", task="зроби ключ", when=when), text("Готово")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", f"нагадай о {target:%H:%M}", rec.emit, rec.confirm, tz="Europe/Kyiv")
    item = agent.storage.list_schedules("owner")[0]
    assert item["next_run"] == target.timestamp()  # саме 14:00 за Києвом, а не за UTC сервера
    assert item["tz"] == "Europe/Kyiv"
    assert f"{target:%Y-%m-%d %H:%M}" in describe(item)
    assert "(Europe/Kyiv)" in llm.calls[1]["messages"][-1]["content"]


def test_daily_repeat_keeps_local_hour_across_dst():
    # 25.10.2026 Київ переходить на зимовий час (UTC+3 → UTC+2)
    before = datetime(2026, 10, 24, 8, 0, tzinfo=KYIV)
    nxt = datetime.fromtimestamp(next_after(before.timestamp(), "daily", before.timestamp(), KYIV), KYIV)
    assert (nxt.day, nxt.hour, nxt.minute) == (25, 8, 0)
    assert before.utcoffset() == timedelta(hours=3) and nxt.utcoffset() == timedelta(hours=2)


def test_ws_task_passes_timezone(cfg):
    client, agent = _client(cfg, [text("ок")])
    with client, client.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "task", "text": "привіт", "session_id": "t", "tz": "Europe/Kyiv"})
        _until(ws, "final")
    assert agent.storage.user_tz("owner") == "Europe/Kyiv"
    assert "Europe/Kyiv" in agent.llm.calls[0]["messages"][0]["content"]
