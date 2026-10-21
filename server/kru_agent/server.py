"""HTTP/WebSocket-сервер агента.

Протокол WebSocket (/ws?token=...), JSON-повідомлення.

Клієнт → сервер:
  {"type": "task", "text": "...", "session_id": "default", "think": true, "auto_files": false, "tz": "Europe/Kyiv"}
      retry: true — «Повторити»: останнє повідомлення чату виконується заново, попередня спроба прибирається
      think — true/false, або "auto" (типово): міркувати лише над складними задачами — код, розрахунки, аналіз,
      плани, після помилки інструмента; final містить stats (секунди, кроки, міркування, т/с); auto_files — запис файлів у робочій теці без запиту (копії в .kru-backup);
      tz — часовий пояс користувача (IANA, напр. "Europe/Kyiv"): час у розмові й розкладі — саме в ньому
  {"type": "confirm_response", "id": "<confirm id>", "approved": true, "scope": "task"}
      scope "task" — дозволяти цей інструмент до кінця задачі (лише запис файлів у робочій теці)
  {"type": "cancel"}
  {"type": "reset", "session_id": "default"}  — видалити розмову
  {"type": "upload", "id": "...", "name": "звіт.pdf", "data": "<base64>"}  — збереже у <тека користувача>/uploads/
  {"type": "file_get", "id": "...", "path": "звіт.pdf"}  — завантажити файл, який агент передав (send_file)
  Чати зберігаються автоматично (session_id — це id чату; новий чат = новий session_id від клієнта):
  {"type": "chat_list"} | {"type": "chat_open", "id"} | {"type": "chat_delete", "id"}
  {"type": "chat_rename", "id", "title"} | {"type": "chat_export", "id"}  — Markdown розмови
  {"type": "chat_search", "query": "відпустка море"}  — пошук за назвою й текстом розмов
  {"type": "voice_in", "id": "...", "data": "<base64 WAV/OGG>", "wake": "detected"}  — розпізнати сказане (Whisper);
      wake=detected — режим «завжди слухаю»: фразу-активатор («Чуєш, Кру») уже впізнала модель на ПК —
      звертання й ім'я прибираються; лише фраза без питання → wake_only;
      wake=followup — уточнення без імені: те, що Whisper «чує» в шумі, повертається як ignored
  {"type": "wake_model"}  — модель своєї фрази для робота (kru wakeword train) → wake_model з files (base64)
  {"type": "tts", "id": "...", "text": "...", "voice": "Speaker_67", "effect": "natural", "speed": 1.0}  — озвучити
      (HolosTTS); voice/effect/speed необов'язкові: типово — з налаштувань сервера (KRU_TTS_VOICE, KRU_VOICE_EFFECT)
      stream=true — частинами по реченнях: кілька tts_audio з part (0, 1, …) і last; {"type": "tts_stop", "id"} — досить
  {"type": "ping"}

Задача не скасовується, коли з'єднання рветься: вона доробляється на сервері, а після перепідключення
клієнт відкриває чат (chat_open) і знову отримує її події та непідтверджені запити.

Сервер → клієнт:
  {"type": "hello", "name": "Kru", "model": ..., "version": ..., "user": "owner", "role": "admin",
   "voice": {"stt": bool, "tts": bool, "engine": "holos|", "voices": [назви голосів], "effects": {id: назва}}}
  {"type": "status", "state": "thinking|working|waiting_confirm|idle", ...}
  {"type": "thinking", "text": "..."}         — потік міркувань моделі
  {"type": "token", "text": "..."}            — потік тексту відповіді (чернетка)
  {"type": "tool_call", "id", "name", "args", "summary"}
  {"type": "tool_result", "id", "name", "ok", "preview"}
  {"type": "confirm_request", "id", "tool", "summary"}
  {"type": "plan", "steps": [{title, status: pending|in_progress|done|skipped}]}  — чек-лист великої задачі
  {"type": "progress", "tool", "text"}       — проміжний стан довгого інструмента / субагента
  події субагента (delegate) мають "sub": true
  {"type": "final", "text": "..."}
  {"type": "uploaded", "id", "name", "path", "size"} | {"type": "upload_error", "id", "name", "message"}
  {"type": "file", "path", "name", "size"}  — агент передав файл (send_file): у чаті — кнопка завантаження
  {"type": "file_data", "id", "path", "name", "size", "data": "<base64>"} | {"type": "file_error", "id", "path", "message"}
  {"type": "chat_renamed", "id", "title"} | {"type": "chat_list", "chats": [{id, title, count, created_at, updated_at}]}
  {"type": "chat_opened", "id", "title", "transcript": [{role, text, file?: {path, name}}], "plan": [...], "running": bool}
      running — у цьому чаті ще виконується задача (наприклад, після розриву з'єднання): далі прийдуть її події
  {"type": "chat_export", "title", "markdown"}
  {"type": "chat_search", "query", "chats": [{id, title, count, created_at, updated_at, snippet}]}
  {"type": "chat_error", "message"}
  {"type": "voice_text", "id", "text", "ignored"?: true, "wake_only"?: true} | {"type": "voice_error", "id", "message"}
      ignored — схоже на шум (heard — що почуто); wake_only — лише звертання без питання
  {"type": "tts_audio", "id", "format": "wav", "data": "<base64>"} | {"type": "tts_error", "id", "message"}
  {"type": "error", "message": "..."}
  {"type": "pong"}
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
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
from .scheduler import Scheduler
from .tools import ToolError
from .tools.files import shared_file
from .users import User, UserStore, workspace_for
from .voice import MAX_AUDIO, WAKE_PROMPT, VoiceError, is_noise_text, strip_detected

log = logging.getLogger("kru.server")

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
        app.state.hub = TaskHub()
        app.state.scheduler = Scheduler(
            app.state.agent.storage,
            lambda item, chat_id: run_scheduled(app.state.agent, app.state.hub, users, item, chat_id),
            lambda owner: app.state.hub.get(owner) is not None,
        )
        app.state.scheduler.start()
        # модель розпізнавання — у фоні одразу, щоб перше голосове не чекало її завантаження
        app.state.stt_preload = asyncio.create_task(asyncio.to_thread(app.state.agent.voice.preload))
        app.state.telegram = None
        if cfg.telegram_bot_token and cfg.telegram_poll:
            from .telegram_bot import TelegramGateway

            app.state.telegram = TelegramGateway(app.state.agent, app.state.hub, users)
            app.state.telegram.start()
            log.info("Telegram-бот слухає повідомлення")
        health = await app.state.agent.llm.health()
        if not health.get("ok"):
            log.warning("LLM-сервер недоступний: %s", health.get("error"))
        elif not health.get("model_present"):
            if cfg.backend == "openai":
                log.warning("LLM-сервер віддає %s, а KRU_MODEL=%s (перевір --alias)", health.get("served"), cfg.model)
            else:
                log.warning("Модель %s не знайдена в Ollama. Виконай: ollama pull %s", cfg.model, cfg.model)
        if not cfg.token and not users.list():
            log.warning("KRU_TOKEN не задано і користувачів немає — сервер доступний без авторизації!")
        yield
        await app.state.scheduler.stop()
        if app.state.telegram:
            await app.state.telegram.stop()
        await app.state.agent.close()

    app = FastAPI(title="Kru", version=__version__, lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        ag: Agent = request.app.state.agent
        return {"ok": True, "version": __version__, "backend": ag.config.backend, "llm": await ag.llm.health()}

    @app.post("/api/task")
    async def api_task(req: TaskRequest, request: Request) -> dict[str, Any]:
        """Неінтерактивний запуск: небезпечні дії відхиляються (якщо не KRU_AUTO_APPROVE)."""
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
        token = ws.query_params.get("token") or ws.headers.get("x-kru-token")
        await ws.accept()
        user = users.authenticate(token)
        if user is None:
            # приймаємо і одразу закриваємо з кодом 4401 — клієнт відрізнить «невірний токен» від інших 403
            await ws.close(code=4401, reason="unauthorized")
            return
        await Connection(ws, ws.app.state.agent, user, ws.app.state.hub).serve()

    return app


class RunningTask:
    """Задача користувача, що живе незалежно від з'єднання: події йдуть усім підписаним підключенням."""

    CONFIRM_TIMEOUT = 600

    def __init__(self, owner: str, session_id: str, scheduled: str = "") -> None:
        self.owner = owner
        self.session_id = session_id
        # заплановане завдання: користувач у цей час у іншому чаті (або його немає) — йому йдуть
        # лише запити на дозвіл і підсумкове сповіщення, а не весь потік подій
        self.scheduled = scheduled
        self.task: asyncio.Task | None = None
        self.subscribers: set[Connection] = set()
        self.pending: dict[str, tuple[asyncio.Future[bool | str], dict[str, Any]]] = {}
        self.last_status: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def emit(self, ev: dict[str, Any]) -> None:
        if ev.get("type") == "status":
            self.last_status = ev
        if self.scheduled:
            if ev.get("type") != "confirm_request":
                return
            ev = {**ev, "scheduled": self.scheduled}
        for conn in list(self.subscribers):
            await conn.send(ev)

    async def confirm(self, tool: str, summary: str, args: dict[str, Any]) -> bool | str:
        cid = uuid.uuid4().hex[:10]
        fut: asyncio.Future[bool | str] = asyncio.get_running_loop().create_future()
        ev = {"type": "confirm_request", "id": cid, "tool": tool, "summary": summary}
        if self.scheduled:
            ev["scheduled"] = self.scheduled
        self.pending[cid] = (fut, ev)
        await self.emit(ev)
        try:
            # без підключених клієнтів теж чекаємо: користувач може перепідключитися й відповісти
            return await asyncio.wait_for(fut, timeout=self.CONFIRM_TIMEOUT)
        except asyncio.TimeoutError:
            return False
        finally:
            self.pending.pop(cid, None)

    def answer(self, cid: str, approved: bool, scope: str = "") -> None:
        item = self.pending.get(cid)
        if item and not item[0].done():
            item[0].set_result("task" if approved and scope == "task" else approved)

    def deny_all(self) -> None:
        for fut, _ in self.pending.values():
            if not fut.done():
                fut.set_result(False)

    async def attach(self, conn: "Connection") -> None:
        """Підключення (часто після розриву) знову стежить за задачею: поточний стан і відкриті запити."""
        self.subscribers.add(conn)
        if self.last_status and not self.scheduled:
            await conn.send(self.last_status)
        for _, ev in list(self.pending.values()):
            await conn.send(ev)


class TaskHub:
    """Активні задачі за користувачами (одна на користувача — модель однаково обробляє запити по черзі)."""

    def __init__(self) -> None:
        self.tasks: dict[str, RunningTask] = {}
        self.connections: dict[str, set[Connection]] = {}  # відкриті підключення кожного користувача

    async def notify(self, owner: str, ev: dict[str, Any]) -> None:
        for conn in list(self.connections.get(owner, ())):
            await conn.send(ev)

    def get(self, owner: str) -> RunningTask | None:
        rt = self.tasks.get(owner)
        return rt if rt and rt.running else None


class Connection:
    """Одне WebSocket-підключення користувача. Задачі живуть у TaskHub і переживають розрив з'єднання."""

    def __init__(self, ws: WebSocket, agent: Agent, user: User | None = None, hub: TaskHub | None = None) -> None:
        self.ws = ws
        self.agent = agent
        self.user = user or User("owner", "admin")
        self.hub = hub or TaskHub()
        self._send_lock = asyncio.Lock()
        self.closed = False
        self._bg: set[asyncio.Task] = set()
        self._tts_now = None  # id озвучення, що зараз іде частинами (новий запит чи tts_stop його скасовують)

    async def send(self, ev: dict[str, Any]) -> None:
        if self.closed:
            return
        async with self._send_lock:
            try:
                await self.ws.send_json(ev)
            except Exception:  # noqa: BLE001
                self.closed = True

    async def _run_task(self, rt: RunningTask, text: str, think: bool | None = None, auto_files: bool = False,
                        tz: str | None = None, retry: bool = False) -> None:
        try:
            await self.agent.run(rt.session_id, text, rt.emit, rt.confirm, self.user, think, auto_files, tz, retry)
        except ValueError as e:  # «Повторити» в порожньому чаті
            await rt.emit({"type": "error", "message": str(e)})
            await rt.emit({"type": "status", "state": "idle"})
        except asyncio.CancelledError:
            await rt.emit({"type": "final", "text": "⏹ Задачу скасовано."})
            await rt.emit({"type": "status", "state": "idle"})
        except Exception as e:  # noqa: BLE001
            log.exception("Помилка під час виконання задачі")
            await rt.emit({"type": "error", "message": str(e)})
            await rt.emit({"type": "status", "state": "idle"})
        finally:
            rt.subscribers.clear()
            if self.hub.tasks.get(rt.owner) is rt:
                del self.hub.tasks[rt.owner]

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

    async def _send_file(self, msg: dict[str, Any]) -> None:
        """Файл із робочої теки користувача (той, що агент передав через send_file) — на ПК одним кліком."""
        fid, path = msg.get("id"), str(msg.get("path") or "")
        try:
            p, shown = shared_file(self.agent.make_context(self.user), path)
            data = await asyncio.to_thread(p.read_bytes)
        except (ToolError, OSError) as e:
            await self.send({"type": "file_error", "id": fid, "path": path, "message": str(e)})
            return
        log.info("Віддаю файл %s (%d байт)", shown, len(data))
        await self.send({"type": "file_data", "id": fid, "path": shown, "name": p.name, "size": len(data),
                         "data": base64.b64encode(data).decode("ascii")})

    async def _wake_model(self) -> None:
        """Модель своєї фрази-активатора для робота (kru wakeword train): класифікатор і мережі відбитків."""
        from . import wakeword

        files = await asyncio.to_thread(wakeword.bundle, self.agent.config)
        meta = wakeword.info(self.agent.config) if files else None
        if not files or not meta:
            await self.send({"type": "wake_model", "error": "на сервері ще не навчено фразу: kru wakeword train"})
            return
        await self.send({"type": "wake_model", "phrase": meta.get("phrase"), "version": meta.get("version"),
                         "threshold": meta.get("threshold"),
                         "files": {k: base64.b64encode(v).decode("ascii") for k, v in files.items()}})

    async def _voice(self, kind: str, msg: dict[str, Any]) -> None:
        """Голос: запис з мікрофона → текст (voice_in) або текст відповіді → WAV (tts)."""
        vid, voice = msg.get("id"), self.agent.voice
        if kind == "tts" and msg.get("stream"):
            self._tts_now = vid
            parts = voice.speak_parts(str(msg.get("text") or ""), msg.get("voice"), msg.get("effect"),
                                      msg.get("speed"))
            try:
                async with contextlib.aclosing(parts):
                    i = 0
                    async for audio, last in parts:
                        if self._tts_now != vid:
                            return  # користувач перебив або попросив озвучити інше
                        await self.send({"type": "tts_audio", "id": vid, "format": "wav", "part": i, "last": last,
                                         "data": base64.b64encode(audio).decode("ascii")})
                        i += 1
            except VoiceError as e:
                await self.send({"type": "tts_error", "id": vid, "message": str(e)})
            return
        if kind == "tts":
            try:
                audio = await voice.speak(str(msg.get("text") or ""), msg.get("voice"), msg.get("effect"),
                                          msg.get("speed"))
            except VoiceError as e:
                await self.send({"type": "tts_error", "id": vid, "message": str(e)})
                return
            await self.send({"type": "tts_audio", "id": vid, "format": "wav",
                             "data": base64.b64encode(audio).decode("ascii")})
            return
        try:
            raw = str(msg.get("data", ""))
            if len(raw) > MAX_AUDIO * 4 // 3 + 4:
                raise VoiceError("Запис задовгий")
            data = base64.b64decode(raw, validate=True)
            detected = msg.get("wake") == "detected"  # фразу-активатор уже впізнала своя модель на ПК
            followup = msg.get("wake") == "followup"  # уточнення без імені: на шумі Whisper любить «вигадувати»
            # підказуємо Whisper, як пишеться ім'я (не нав'язуючи його на початок фрази)
            text = await voice.transcribe(data, WAKE_PROMPT if detected else None, strict=followup)
        except (binascii.Error, ValueError):
            await self.send({"type": "voice_error", "id": vid, "message": "пошкоджений запис"})
            return
        except VoiceError as e:
            await self.send({"type": "voice_error", "id": vid, "message": str(e)})
            return
        reply: dict[str, Any] = {"type": "voice_text", "id": vid, "text": text}
        if detected:
            rest = strip_detected(text)
            reply["text"] = rest
            if not rest:
                reply["wake_only"] = True  # лише фраза — робот слухатиме саме питання
        if followup and is_noise_text(text):
            log.info("Режим «Кру»: уточнення схоже на шум — ігнорую")
            reply.update(text="", ignored=True, heard=text[:200])
        log.info("Розпізнано голос: %d символів", len(reply["text"]))
        await self.send(reply)

    async def _chats(self, kind: str, msg: dict[str, Any]) -> None:
        """Чати (зберігаються автоматично): список / відкрити / перейменувати / видалити / експорт."""
        ag, owner = self.agent, self.user.name
        chat_id = str(msg.get("id") or msg.get("session_id") or "")
        rt = self.hub.get(owner)
        try:
            if kind in {"chat_rename", "chat_save"}:  # chat_save — від старих клієнтів: лише назва
                title = msg.get("title")
                if kind == "chat_save" and not title:
                    raise ValueError("Чати тепер зберігаються автоматично")
                await self.send({"type": "chat_renamed", **ag.rename_chat(chat_id, title, owner)})
            elif kind == "chat_open":
                running = rt is not None and rt.session_id == chat_id
                await self.send({"type": "chat_opened", **ag.open_chat(chat_id, owner), "running": running})
                if running:
                    await rt.attach(self)
            elif kind == "chat_delete":
                if rt is not None and chat_id == rt.session_id:
                    raise ValueError("Спершу зупини задачу в цьому чаті")
                ag.delete_chat(chat_id, owner)
            elif kind == "chat_export":
                await self.send({"type": "chat_export", **ag.export_chat(chat_id, owner)})
            elif kind == "chat_search":
                query = str(msg.get("query") or "")[:200]
                found = await asyncio.to_thread(ag.search_chats, query, owner)
                await self.send({"type": "chat_search", "query": query, "chats": found})
            if kind in {"chat_list", "chat_delete", "chat_rename", "chat_save"}:
                await self.send({"type": "chat_list", "chats": ag.list_chats(owner)})
        except (ValueError, TypeError, KeyError) as e:
            await self.send({"type": "chat_error", "message": str(e)})

    async def serve(self) -> None:
        cfg = self.agent.config
        vision = await self.agent.refresh_vision(force=True)
        await self.agent.voice.refresh_voices()
        await self.send({"type": "hello", "name": cfg.agent_name, "model": cfg.model, "version": __version__,
                         "vision": vision, "user": self.user.name, "role": self.user.role,
                         "voice": self.agent.voice.caps()})
        self.hub.connections.setdefault(self.user.name, set()).add(self)
        if (rt := self.hub.get(self.user.name)) and rt.scheduled:
            await rt.attach(self)  # заплановане завдання може чекати на дозвіл
        try:
            while True:
                msg = await self.ws.receive_json()
                kind = msg.get("type")
                if kind == "task":
                    text = str(msg.get("text", "")).strip()
                    retry = msg.get("retry") is True
                    if not text and not retry:
                        continue
                    if busy := self.hub.get(self.user.name):
                        msg = (f"Зараз виконується заплановане завдання «{busy.scheduled[:60]}». Зачекай або зупини його (■)."
                               if busy.scheduled else "Я ще працюю над попередньою задачею. Скасуй її або зачекай.")
                        await self.send({"type": "error", "message": msg})
                        continue
                    think = msg.get("think")
                    think = think if isinstance(think, bool) else None
                    rt = RunningTask(self.user.name, str(msg.get("session_id") or "default"))
                    rt.subscribers.add(self)
                    self.hub.tasks[self.user.name] = rt
                    auto_files = msg.get("auto_files") is True
                    tz = msg.get("tz") if isinstance(msg.get("tz"), str) else None
                    rt.task = asyncio.create_task(self._run_task(rt, text, think, auto_files, tz, retry))
                elif kind == "confirm_response":
                    if rt := self.hub.get(self.user.name):
                        rt.answer(str(msg.get("id")), bool(msg.get("approved")), str(msg.get("scope") or ""))
                elif kind == "cancel":
                    if rt := self.hub.get(self.user.name):
                        rt.deny_all()
                        rt.task.cancel()
                elif kind == "reset":
                    self.agent.reset_session(str(msg.get("session_id") or "default"), self.user.name)
                    await self.send({"type": "final", "text": "🧹 Розмову очищено."})
                elif kind in {"chat_save", "chat_rename", "chat_list", "chat_open", "chat_delete", "chat_export",
                              "chat_search"}:
                    await self._chats(kind, msg)
                elif kind == "upload":
                    await self._save_upload(msg)
                elif kind == "file_get":
                    await self._send_file(msg)
                elif kind == "wake_model":
                    await self._wake_model()
                elif kind == "tts_stop":
                    if self._tts_now == msg.get("id"):
                        self._tts_now = None
                elif kind in {"voice_in", "tts"}:
                    # розпізнавання/озвучення — секунди роботи CPU: не блокуємо решту повідомлень
                    task = asyncio.create_task(self._voice(kind, msg))
                    self._bg.add(task)
                    task.add_done_callback(self._bg.discard)
                elif kind == "ping":
                    await self.send({"type": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Помилка WebSocket")
        finally:
            # задачу НЕ скасовуємо: вона доробиться, а клієнт підхопить її після перепідключення
            self.closed = True
            for rt in self.hub.tasks.values():
                rt.subscribers.discard(self)
            self.hub.connections.get(self.user.name, set()).discard(self)


SCHEDULED_PREFIX = (
    "⏰ Заплановане завдання (користувач поставив його раніше і зараз, можливо, не біля комп'ютера — "
    "не став уточнювальних питань, просто виконай; якщо в завданні сказано надіслати в Telegram — надішли):\n"
)


async def run_scheduled(agent: Agent, hub: TaskHub, users: UserStore, item: dict[str, Any], chat_id: str) -> str:
    """Виконати заплановане завдання від імені його власника в окремому чаті й повідомити про результат."""
    user = users.get(item["owner"])
    if user is None:
        raise RuntimeError("користувача видалено")
    title = f"⏰ {' '.join(item['task'].split())[:60]}"
    rt = RunningTask(user.name, chat_id, scheduled=title)
    rt.subscribers = set(hub.connections.get(user.name, ()))
    rt.task = asyncio.current_task()
    hub.tasks[user.name] = rt
    try:
        answer = await agent.run(chat_id, SCHEDULED_PREFIX + item["task"], rt.emit, rt.confirm, user,
                                 tz=item.get("tz") or None)
        agent.storage.rename_chat(chat_id, title, user.name)
        await hub.notify(user.name, {"type": "notify", "title": title, "text": answer[:800], "chat_id": chat_id})
        return answer
    except Exception as e:
        await hub.notify(user.name, {"type": "notify", "title": title, "text": f"Не вдалося: {e}", "chat_id": chat_id,
                                     "error": True})
        raise
    finally:
        rt.deny_all()
        if hub.tasks.get(user.name) is rt:
            del hub.tasks[user.name]


def run_server(config: Config) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(config), host=config.host, port=config.port, ws_ping_interval=20, ws_ping_timeout=60,
                ws_max_size=48 * 1024 * 1024)  # base64 від файлу до 25 МБ
