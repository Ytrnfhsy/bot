from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bonsai_agent.agent import Agent
from bonsai_agent.server import create_app

from .conftest import FakeLLM, call, text


def _client(cfg, script):
    cfg.token = "secret"
    agent = Agent(cfg, llm=FakeLLM(script))
    return TestClient(create_app(cfg, agent=agent)), agent


def _until(ws, kind):
    seen = []
    while True:
        ev = ws.receive_json()
        seen.append(ev)
        if ev["type"] == kind:
            return ev, seen


def test_ws_task_with_confirmation(cfg):
    client, agent = _client(cfg, [call("write_file", path="hi.txt", content="hello"), text("Файл створено ✅")])
    with client, client.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "task", "text": "створи файл", "session_id": "t"})
        req, _ = _until(ws, "confirm_request")
        assert "hi.txt" in req["summary"]
        ws.send_json({"type": "confirm_response", "id": req["id"], "approved": True})
        final, seen = _until(ws, "final")
        assert final["text"] == "Файл створено ✅"
        assert any(e["type"] == "tool_result" and e["ok"] for e in seen)
    assert (cfg.workspace / "hi.txt").read_text() == "hello"


def test_ws_rejects_bad_token(cfg):
    client, _ = _client(cfg, [])
    with client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws?token=wrong") as ws:
                ws.receive_json()


def test_rest_task_and_health(cfg):
    client, _ = _client(cfg, [call("run_shell", command="ls"), text("не дозволено, тож ось відповідь")])
    with client:
        assert client.get("/health").json()["ok"]
        assert client.post("/api/task", json={"text": "ls"}).status_code == 401
        r = client.post("/api/task", json={"text": "ls"}, headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200
        assert r.json()["answer"].startswith("не дозволено")
