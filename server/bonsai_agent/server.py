"""HTTP/WebSocket-сервер агента.

Протокол WebSocket (/ws?token=...), JSON-повідомлення.

Клієнт → сервер:
  {"type": "task", "text": "...", "session_id": "default"}
  {"type": "confirm_response", "id": "<confirm id>", "approved": true}
  {"type": "cancel"}
  {"type": "reset", "session_id": "default"}  — видалити розмову
  {"type": "upload", "id": "...", "name": "звіт.pdf", "data": "<base64>"}  — збереже у <тека користувача>/uploads/
  Чати зберігаються автоматично (session_id — це id чату; новий чат = новий session_id від клієнта):
  {"type": "chat_list"} | {"type": "chat_open", "id"} | {"type": "chat_delete", "id"}
  {"type": "chat_rename", "id", "title"} | {"type": "chat_export", "id"}  — Markdown розмови
  {"type": "ping"}

Сервер → клієнт:
  {"type": "hello", "name": "Kru", "model": ..., "version": ..., "user": "owner", "role": "admin"}
  {"type": "status", "state": "thinking|working|waiting_confirm|idle", ...}
  {"type": "thinking", "text": "..."}         — потік міркувань моделі
  {"type": "token", "text": "..."}            — потік тексту відповіді (чернетка)
  {"type": "tool_call", "id", "name", "args", "summary"}
  {"type": "tool_result", "id", "name", "ok", "preview"}
  {"type": "confirm_request", "id", "tool", "summary"}
  {"type": "final", "text": "..."}
  {"type": "uploaded", "id", "name", "path", "size"} | {"type": "upload_error", "id", "name", "message"}
  {"type": "chat_renamed", "id", "title"} | {"type": "chat_list", "chats": [{id, title, count, created_at, updated_at}]}
  {"type": "chat_opened", "id", "title", "transcript": [{role, text}]} | {"type": "chat_export", "title", "markdown"}
  {"type": "chat_error", "message"}
  {"type": "error", "message": "..."}
  {"type": "pong"}
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import __version__
from .agent import Agent
from .config import Config
from .users import User, UserStore, workspace_for

log = logging.getLogger("bonsai.server")

MAX_UPLOAD = 25 * 1024 * 1024  # файли, перетягнуті на робота


class TaskRequest(BaseModel):
    text: str
    session_id: str = "api"


def create_app(config: Config | None = None, agent: Agent | None = None) -> FastAPI:
    cfg = config or Config.from_env()
    users = UserStore(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.agent = agent or Agent(cfg)
        health = await app.state.agent.llm.health()
        if not health.get("ok"):
            log.warning("LLM-сервер недоступний: %s", health.get("error"))
        elif not health.get("model_present"):
            if cfg.backend == "openai":
                log.warning("LLM-сервер віддає %s, а BONSAI_MODEL=%s (перевір --alias)", health.get("served"), cfg.model)
            else:
                log.warning("Модель %s не знайдена в Ollama. Виконай: ollama pull %s", cfg.model, cfg.model)
        if not cfg.token and not users.list():
            log.warning("BONSAI_TOKEN не задано і користувачів немає — сервер доступний без авторизації!")
        yield
        await app.state.agent.close()

    app = FastAPI(title="Bonsai Agent", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        ag: Agent = request.app.state.agent
        return {"ok": True, "version": __version__, "backend": ag.config.backend, "llm": await ag.llm.health()}

    @app.post("/api/task")
    async def api_task(req: TaskRequest, request: Request) -> dict[str, Any]:
        """Неінтерактивний запуск: небезпечні дії відхиляються (якщо не BONSAI_AUTO_APPROVE)."""
        auth = request.headers.get("authorization", "")
        user = users.authenticate(auth.removeprefix("Bearer ").strip() or None)
        if user is None:
            raise HTTPException(status_code=401, detail="Невірний токен")
        ag: Agent = request.app.state.agent
        events: list[dict[str, Any]] = []

        async def emit(ev: dict[str, Any]) -> None:
            if ev["type"] in {"tool_call", "tool_result"}:
                events.append(ev)

        async def deny(*_: Any) -> bool:
            return False

        answer = await ag.run(req.session_id, req.text, emit, deny, user)
        return {"answer": answer, "events": events}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        token = ws.query_params.get("token") or ws.headers.get("x-bonsai-token")
        await ws.accept()
        user = users.authenticate(token)
        if user is None:
            # приймаємо і одразу закриваємо з кодом 4401 — клієнт відрізнить «невірний токен» від інших 403
            await ws.close(code=4401, reason="unauthorized")
            return
        await Connection(ws, ws.app.state.agent, user).serve()

    return app


class Connection:
    """Одне WebSocket-підключення: одна активна задача, підтвердження через futures."""

    def __init__(self, ws: WebSocket, agent: Agent, user: User | None = None) -> None:
        self.ws = ws
        self.agent = agent
        self.user = user or User("owner", "admin")
        self.task: asyncio.Task | None = None
        self._task_session = ""
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
            await self.agent.run(session_id, text, self.send, self.confirm, self.user)
        except asyncio.CancelledError:
            await self.send({"type": "final", "text": "⏹ Задачу скасовано."})
            await self.send({"type": "status", "state": "idle"})
        except Exception as e:  # noqa: BLE001
            log.exception("Помилка під час виконання задачі")
            await self.send({"type": "error", "message": str(e)})
            await self.send({"type": "status", "state": "idle"})

    async def _save_upload(self, msg: dict[str, Any]) -> None:
        """Файл, перетягнутий на робота: зберігаємо у workspace/uploads/ (без перезапису наявних)."""
        uid, raw_name = msg.get("id"), str(msg.get("name") or "file")
        name = Path(raw_name.replace("\\", "/")).name.lstrip(".")[:120] or "file"
        name = re.sub(r"\s+", "_", name)  # без пробілів: шлях однозначно розпізнається в тексті задачі
        try:
            data = base64.b64decode(str(msg.get("data", "")), validate=True)
        except (binascii.Error, ValueError):
            await self.send({"type": "upload_error", "id": uid, "name": name, "message": "пошкоджені дані"})
            return
        if len(data) > MAX_UPLOAD:
            await self.send({"type": "upload_error", "id": uid, "name": name, "message": "файл більший за 25 МБ"})
            return
        ws_dir = workspace_for(self.agent.config, self.user)
        target = ws_dir / "uploads" / name
        stem, suffix, n = target.stem, target.suffix, 1
        while target.exists():
            n += 1
            target = target.with_name(f"{stem}-{n}{suffix}")

        def write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        await asyncio.to_thread(write)
        rel = target.relative_to(ws_dir).as_posix()
        log.info("Отримано файл %s (%d байт)", rel, len(data))
        await self.send({"type": "uploaded", "id": uid, "name": name, "path": rel, "size": len(data)})

    async def _chats(self, kind: str, msg: dict[str, Any]) -> None:
        """Чати (зберігаються автоматично): список / відкрити / перейменувати / видалити / експорт."""
        ag, owner = self.agent, self.user.name
        chat_id = str(msg.get("id") or msg.get("session_id") or "")
        busy = self.task is not None and not self.task.done()
        try:
            if kind in {"chat_rename", "chat_save"}:  # chat_save — від старих клієнтів: лише назва
                title = msg.get("title")
                if kind == "chat_save" and not title:
                    raise ValueError("Чати тепер зберігаються автоматично")
                await self.send({"type": "chat_renamed", **ag.rename_chat(chat_id, title, owner)})
            elif kind == "chat_open":
                await self.send({"type": "chat_opened", **ag.open_chat(chat_id, owner)})
            elif kind == "chat_delete":
                if busy and chat_id == self._task_session:
                    raise ValueError("Спершу зупини задачу в цьому чаті")
                ag.delete_chat(chat_id, owner)
            elif kind == "chat_export":
                await self.send({"type": "chat_export", **ag.export_chat(chat_id, owner)})
            if kind in {"chat_list", "chat_delete", "chat_rename", "chat_save"}:
                await self.send({"type": "chat_list", "chats": ag.list_chats(owner)})
        except (ValueError, TypeError, KeyError) as e:
            await self.send({"type": "chat_error", "message": str(e)})

    def _cancel_pending(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result(False)

    async def serve(self) -> None:
        cfg = self.agent.config
        vision = await self.agent.refresh_vision(force=True)
        await self.send({"type": "hello", "name": cfg.agent_name, "model": cfg.model, "version": __version__,
                         "vision": vision, "user": self.user.name, "role": self.user.role})
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
                    self._task_session = str(msg.get("session_id") or "default")
                    self.task = asyncio.create_task(self._run_task(text, self._task_session))
                elif kind == "confirm_response":
                    fut = self.pending.get(str(msg.get("id")))
                    if fut and not fut.done():
                        fut.set_result(bool(msg.get("approved")))
                elif kind == "cancel":
                    self._cancel_pending()
                    if self.task and not self.task.done():
                        self.task.cancel()
                elif kind == "reset":
                    self.agent.reset_session(str(msg.get("session_id") or "default"), self.user.name)
                    await self.send({"type": "final", "text": "🧹 Розмову очищено."})
                elif kind in {"chat_save", "chat_rename", "chat_list", "chat_open", "chat_delete", "chat_export"}:
                    await self._chats(kind, msg)
                elif kind == "upload":
                    await self._save_upload(msg)
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
    uvicorn.run(create_app(config), host=config.host, port=config.port, ws_ping_interval=20, ws_ping_timeout=60,
                ws_max_size=48 * 1024 * 1024)  # base64 від файлу до 25 МБ
