"""HTTP/WebSocket-сервер агента.

Протокол WebSocket (/ws?token=...), JSON-повідомлення.

Клієнт → сервер:
  {"type": "task", "text": "...", "session_id": "default"}
  {"type": "confirm_response", "id": "<confirm id>", "approved": true}
  {"type": "cancel"}
  {"type": "reset", "session_id": "default"}
  {"type": "ping"}

Сервер → клієнт:
  {"type": "hello", "model": ..., "version": ...}
  {"type": "status", "state": "thinking|working|waiting_confirm|idle", ...}
  {"type": "thinking", "text": "..."}         — потік міркувань моделі
  {"type": "token", "text": "..."}            — потік тексту відповіді (чернетка)
  {"type": "tool_call", "id", "name", "args", "summary"}
  {"type": "tool_result", "id", "name", "ok", "preview"}
  {"type": "confirm_request", "id", "tool", "summary"}
  {"type": "final", "text": "..."}
  {"type": "error", "message": "..."}
  {"type": "pong"}
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import __version__
from .agent import Agent
from .config import Config

log = logging.getLogger("bonsai.server")


def _check_token(cfg: Config, supplied: str | None) -> bool:
    if not cfg.token:
        return True
    return supplied is not None and hmac.compare_digest(cfg.token, supplied)


class TaskRequest(BaseModel):
    text: str
    session_id: str = "api"


def create_app(config: Config | None = None, agent: Agent | None = None) -> FastAPI:
    cfg = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.agent = agent or Agent(cfg)
        health = await app.state.agent.llm.health()
        if not health.get("ok"):
            log.warning("LLM-сервер недоступний: %s", health.get("error"))
        elif not health.get("model_present"):
            log.warning("Модель %s не знайдена в Ollama. Виконай: ollama pull %s", cfg.model, cfg.model)
        if not cfg.token:
            log.warning("BONSAI_TOKEN не задано — сервер доступний без авторизації!")
        yield
        await app.state.agent.close()

    app = FastAPI(title="Bonsai Agent", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        ag: Agent = request.app.state.agent
        return {"ok": True, "version": __version__, "ollama": await ag.llm.health()}

    @app.post("/api/task")
    async def api_task(req: TaskRequest, request: Request) -> dict[str, Any]:
        """Неінтерактивний запуск: небезпечні дії відхиляються (якщо не BONSAI_AUTO_APPROVE)."""
        auth = request.headers.get("authorization", "")
        if not _check_token(cfg, auth.removeprefix("Bearer ").strip() or None):
            raise HTTPException(status_code=401, detail="Невірний токен")
        ag: Agent = request.app.state.agent
        events: list[dict[str, Any]] = []

        async def emit(ev: dict[str, Any]) -> None:
            if ev["type"] in {"tool_call", "tool_result"}:
                events.append(ev)

        async def deny(*_: Any) -> bool:
            return False

        answer = await ag.run(req.session_id, req.text, emit, deny)
        return {"answer": answer, "events": events}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        token = ws.query_params.get("token") or ws.headers.get("x-bonsai-token")
        if not _check_token(cfg, token):
            await ws.close(code=4401, reason="unauthorized")
            return
        await ws.accept()
        await Connection(ws, ws.app.state.agent).serve()

    return app


class Connection:
    """Одне WebSocket-підключення: одна активна задача, підтвердження через futures."""

    def __init__(self, ws: WebSocket, agent: Agent) -> None:
        self.ws = ws
        self.agent = agent
        self.task: asyncio.Task | None = None
        self.pending: dict[str, asyncio.Future[bool]] = {}
        self._send_lock = asyncio.Lock()
        self.closed = False

    async def send(self, ev: dict[str, Any]) -> None:
        if self.closed:
            return
        async with self._send_lock:
            try:
                await self.ws.send_json(ev)
            except Exception:  # noqa: BLE001
                self.closed = True

    async def confirm(self, tool: str, summary: str, args: dict[str, Any]) -> bool:
        cid = uuid.uuid4().hex[:10]
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[cid] = fut
        await self.send({"type": "confirm_request", "id": cid, "tool": tool, "summary": summary})
        try:
            return await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(cid, None)

    async def _run_task(self, text: str, session_id: str) -> None:
        try:
            await self.agent.run(session_id, text, self.send, self.confirm)
        except asyncio.CancelledError:
            await self.send({"type": "final", "text": "⏹ Задачу скасовано."})
            await self.send({"type": "status", "state": "idle"})
        except Exception as e:  # noqa: BLE001
            log.exception("Помилка під час виконання задачі")
            await self.send({"type": "error", "message": str(e)})
            await self.send({"type": "status", "state": "idle"})

    def _cancel_pending(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result(False)

    async def serve(self) -> None:
        await self.send({"type": "hello", "model": self.agent.config.model, "version": __version__})
        try:
            while True:
                msg = await self.ws.receive_json()
                kind = msg.get("type")
                if kind == "task":
                    text = str(msg.get("text", "")).strip()
                    if not text:
                        continue
                    if self.task and not self.task.done():
                        await self.send({"type": "error", "message": "Я ще працюю над попередньою задачею. Скасуй її або зачекай."})
                        continue
                    self.task = asyncio.create_task(self._run_task(text, str(msg.get("session_id") or "default")))
                elif kind == "confirm_response":
                    fut = self.pending.get(str(msg.get("id")))
                    if fut and not fut.done():
                        fut.set_result(bool(msg.get("approved")))
                elif kind == "cancel":
                    self._cancel_pending()
                    if self.task and not self.task.done():
                        self.task.cancel()
                elif kind == "reset":
                    self.agent.reset_session(str(msg.get("session_id") or "default"))
                    await self.send({"type": "final", "text": "🧹 Розмову очищено."})
                elif kind == "ping":
                    await self.send({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Помилка WebSocket")
        finally:
            self.closed = True
            self._cancel_pending()
            if self.task and not self.task.done():
                self.task.cancel()


def run_server(config: Config) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(config), host=config.host, port=config.port, ws_ping_interval=20, ws_ping_timeout=60)
