"""Telegram у дві сторони: пишеш боту — відповідає агент (як у персонажі), дозволи — кнопками.

Сервер слухає бота (long polling), коли задано KRU_TELEGRAM_BOT_TOKEN. Відповідає лише прив'язаним чатам:
прив'язка — одноразовим кодом (`kru telegram link` → боту «/link 123456»).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

from .telegram import TelegramBot, TelegramError
from .tools import ToolError
from .tools.files import shared_file
from .users import workspace_for
from .voice import VoiceError

log = logging.getLogger("kru.telegram")

FILE_TOOLS = {"write_file", "edit_file"}  # для них є кнопка «До кінця задачі»
ACTIVITY = {
    "web_search": "🔎 Шукаю в інтернеті", "fetch_url": "🌐 Читаю сторінку", "deep_research": "📚 Досліджую тему",
    "read_file": "📖 Читаю файл", "list_dir": "📂 Дивлюся файли", "search_files": "📂 Шукаю у файлах",
    "write_file": "✍️ Записую файл", "edit_file": "✍️ Редагую файл", "run_shell": "⚙️ Виконую команду",
    "ssh_run": "🖥 Працюю на віддаленій машині", "ssh_upload": "🖥 Передаю файли на машину",
    "ssh_download": "🖥 Забираю файли з машини",
    "run_python": "🧮 Рахую", "remember": "🧠 Запам'ятовую", "recall": "🧠 Пригадую",
    "view_image": "👁 Дивлюся на зображення", "delegate": "🤝 Працює субагент", "schedule_task": "⏰ Планую",
    "send_telegram": "✈️ Пишу в Telegram", "send_file": "📎 Надсилаю файл",
    "search_chat": "🔎 Шукаю в історії чату", "read_chat": "🔎 Перечитую розмову",
}
HELP = (
    "Я {name} — твій агент. Просто пиши задачу, як у персонажі на ПК: знайти, порахувати, перекласти, "
    "нагадати…\n\nФото, файли й голосові теж можна надсилати.\n\n/new — новий чат\n/stop — зупинити задачу\n/retry — виконати останню задачу заново\n/help — довідка"
)


def new_code() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


class TelegramSink:
    """«Підключення» Telegram-чату до задачі: перетворює події агента на повідомлення бота."""

    THROTTLE = 3.0  # як часто оновлювати повідомлення з ходом роботи

    def __init__(self, gateway: "TelegramGateway", chat_id: str, user=None) -> None:
        self.gw = gateway
        self.chat_id = chat_id
        self.user = user
        self.status_id = 0  # повідомлення «⏳ …», яке оновлюється
        self.status_at = 0.0
        self.closed = False

    async def send(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        bot = self.gw.bot
        try:
            if kind == "tool_call" and ev.get("name") not in {"update_plan", "mark_step"}:
                await self._status(ACTIVITY.get(str(ev.get("name")), f"🔧 {ev.get('name')}"))
            elif kind == "plan":
                done = sum(1 for s in ev.get("steps", []) if s.get("status") in {"done", "skipped"})
                current = next((s["title"] for s in ev.get("steps", []) if s.get("status") == "in_progress"), "")
                if ev.get("steps"):
                    await self._status(f"📋 План {done}/{len(ev['steps'])}" + (f": {current}" if current else ""))
            elif kind == "confirm_request":
                buttons = [("✅ Дозволити", f"c:{ev['id']}:y"), ("❌ Відхилити", f"c:{ev['id']}:n")]
                rows = [buttons]
                if ev.get("tool") in FILE_TOOLS:
                    rows.append([("✅ До кінця задачі", f"c:{ev['id']}:t")])
                head = f"{ev['scheduled']}\n" if ev.get("scheduled") else ""
                await bot.send(self.chat_id, f"⚠ {head}Агент просить дозволу:\n\n{ev.get('summary', '')}", rows)
            elif kind == "file":  # send_file — у Telegram це документ
                await self._status(f"📎 Надсилаю {ev.get('name', 'файл')}")
                try:
                    path = shared_file(self.gw.agent.make_context(self.user), str(ev.get("path", "")))[0]
                except ToolError as e:
                    await bot.send(self.chat_id, f"⚠️ {e}")
                else:
                    await bot.send_file(self.chat_id, path)
            elif kind == "final":
                await self._clear_status()
                await bot.send(self.chat_id, ev.get("text") or "(порожня відповідь)")
            elif kind == "error":
                await self._clear_status()
                await bot.send(self.chat_id, f"⚠️ {ev.get('message', 'помилка')}")
        except TelegramError as e:
            log.warning("Telegram: не вдалося надіслати (%s)", e)

    async def _status(self, text: str) -> None:
        now = time.monotonic()
        if self.status_id and now - self.status_at < self.THROTTLE:
            return
        self.status_at = now
        text = f"⏳ {text}…"
        if self.status_id:
            try:
                await self.gw.bot.edit(self.chat_id, self.status_id, text)
                return
            except TelegramError:
                pass  # повідомлення видалили чи воно застаре — надішлемо нове
        self.status_id = await self.gw.bot.send(self.chat_id, text)

    async def _clear_status(self) -> None:
        if self.status_id:
            try:
                await self.gw.bot.delete(self.chat_id, self.status_id)
            except TelegramError:
                pass
            self.status_id = 0


class TelegramGateway:
    def __init__(self, agent, hub, users, bot: TelegramBot | None = None) -> None:
        self.agent = agent
        self.hub = hub
        self.users = users
        self.config = agent.config
        self.bot = bot or TelegramBot(self.config.telegram_bot_token)
        self._task: asyncio.Task | None = None
        self._jobs: set[asyncio.Task] = set()

    # ---------- цикл опитування ----------
    def start(self) -> None:
        self._task = asyncio.create_task(self._poll())

    async def stop(self) -> None:
        for t in [self._task, *self._jobs]:
            if t and not t.done():
                t.cancel()

    async def _poll(self) -> None:
        offset = None
        while True:
            try:
                updates = await self.bot.updates(offset, timeout=25)
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                log.warning("Telegram: %s — повторю за 10 с", e)
                await asyncio.sleep(10)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                job = asyncio.create_task(self.handle(upd))
                self._jobs.add(job)
                job.add_done_callback(self._jobs.discard)

    async def handle(self, upd: dict[str, Any]) -> None:
        try:
            if "callback_query" in upd:
                await self._on_callback(upd["callback_query"])
            elif "message" in upd:
                await self._on_message(upd["message"])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Telegram: помилка обробки оновлення")

    # ---------- повідомлення ----------
    async def _on_message(self, msg: dict[str, Any]) -> None:
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not chat_id:
            return
        if m := re.fullmatch(r"/link(?:@\w+)?\s+(\d{6})", text):
            return await self._link(chat_id, m.group(1))
        owner = self.agent.storage.telegram_owner(chat_id)
        if owner is None:
            await self.bot.send(chat_id, "🔒 Це приватний бот. Власник може підключити тебе на сервері: "
                                         "kru telegram link — і надіслати сюди /link КОД.")
            return
        user = self.users.get(owner)
        if user is None:
            await self.bot.send(chat_id, "🔒 Користувача видалено на сервері.")
            return
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd in {"/start", "/help"}:
            return await self.bot.send(chat_id, HELP.format(name=self.config.agent_name))
        if cmd == "/new":
            self.agent.storage.set_telegram_session(owner, self._new_session())
            return await self.bot.send(chat_id, "📝 Новий чат. Попередній збережено — його видно в «Усі чати».")
        if cmd == "/stop":
            if rt := self.hub.get(owner):
                rt.deny_all()
                rt.task.cancel()
                return await self.bot.send(chat_id, "⏹ Зупиняю задачу.")
            return await self.bot.send(chat_id, "Зараз нічого не виконується.")

        if voice := msg.get("voice") or msg.get("audio"):
            if self.hub.get(owner):
                return await self.bot.send(chat_id, "⏳ Зараз виконую іншу задачу. Зачекай або надішли /stop.")
            heard = await self._hear(chat_id, voice)
            if not heard:
                return
            text = (text + "\n" + heard).strip() if text else heard

        retry = cmd == "/retry"
        if retry:
            text = "/retry"  # сам текст задачі агент візьме з чату
        files = [] if retry else await self._save_files(msg, user)
        if files:
            text = (text + "\n" if text else "Подивись, будь ласка:\n") + "\n".join(files)
        if not text:
            return
        if busy := self.hub.get(owner):
            what = f"заплановане завдання «{busy.scheduled[:60]}»" if busy.scheduled else "іншу задачу"
            return await self.bot.send(chat_id, f"⏳ Зараз виконую {what}. Зачекай або надішли /stop.")
        await self._run(owner, user, chat_id, text, retry)

    def _session(self, owner: str) -> str:
        sid = self.agent.storage.telegram_session(owner)
        if not sid:
            sid = self._new_session()
            self.agent.storage.set_telegram_session(owner, sid)
        return sid

    @staticmethod
    def _new_session() -> str:
        return f"tg-{uuid.uuid4().hex[:10]}"

    async def _run(self, owner: str, user, chat_id: str, text: str, retry: bool = False) -> None:
        from .server import RunningTask

        sid = self._session(owner)
        rt = RunningTask(owner, sid)
        rt.subscribers.add(TelegramSink(self, chat_id, user))
        rt.task = asyncio.current_task()
        self.hub.tasks[owner] = rt
        typing = asyncio.create_task(self._typing(chat_id))
        try:
            await self.agent.run(sid, text, rt.emit, rt.confirm, user, tz=self.agent.storage.user_tz(owner) or None,
                                 retry=retry)
            if chat := self.agent.storage.load_chat(sid, owner):
                if not chat["title"].startswith("✈️"):
                    self.agent.storage.rename_chat(sid, "✈️ " + chat["title"], owner)
        except asyncio.CancelledError:
            await self.bot.send(chat_id, "⏹ Задачу зупинено.")
        except ValueError as e:  # /retry у порожньому чаті
            await self.bot.send(chat_id, f"⚠️ {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("Telegram: задача впала")
            await self.bot.send(chat_id, f"⚠️ Помилка: {e}")
        finally:
            typing.cancel()
            rt.deny_all()
            if self.hub.tasks.get(owner) is rt:
                del self.hub.tasks[owner]

    async def _typing(self, chat_id: str) -> None:
        while True:
            try:
                await self.bot.typing(chat_id)
            except TelegramError:
                pass
            await asyncio.sleep(4.5)

    async def _hear(self, chat_id: str, voice: dict[str, Any]) -> str:
        """Голосове повідомлення → текст (faster-whisper на сервері); показуємо, що почули."""
        if not self.agent.voice.stt_ok:
            await self.bot.send(chat_id, "🎤 Голосові повідомлення ще не підключено на сервері "
                                         "(deploy/voice.sh install). Напиши, будь ласка, текстом.")
            return ""
        try:
            data, _ = await self.bot.download(voice["file_id"])
            heard = await self.agent.voice.transcribe(data)
        except (TelegramError, VoiceError) as e:
            await self.bot.send(chat_id, f"⚠️ Не вдалося розпізнати голосове: {e}")
            return ""
        if not heard:
            await self.bot.send(chat_id, "🎤 Не почув слів — спробуй ще раз.")
            return ""
        await self.bot.send(chat_id, f"🎤 «{heard}»")
        return heard

    async def _save_files(self, msg: dict[str, Any], user) -> list[str]:
        """Фото/документи з повідомлення → <тека користувача>/uploads/; повертає відносні шляхи."""
        items = []
        if msg.get("photo"):
            items.append((msg["photo"][-1]["file_id"], ""))  # найбільший розмір
        if doc := msg.get("document"):
            items.append((doc["file_id"], doc.get("file_name") or ""))
        saved = []
        ws = workspace_for(self.config, user)
        for file_id, name in items:
            try:
                data, tg_path = await self.bot.download(file_id)
            except TelegramError as e:
                await self.bot.send(str(msg["chat"]["id"]), f"⚠️ Не вдалося отримати файл: {e}")
                continue
            name = Path((name or Path(tg_path).name).replace("\\", "/")).name.lstrip(".") or "file"
            name = re.sub(r"\s+", "_", name)[:120]
            if not Path(name).suffix and Path(tg_path).suffix:
                name += Path(tg_path).suffix
            target = ws / "uploads" / f"tg-{time.strftime('%Y%m%d-%H%M%S')}-{name}"

            def write(target=target, data=data) -> None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

            await asyncio.to_thread(write)
            saved.append(target.relative_to(ws).as_posix())
        return saved

    async def _link(self, chat_id: str, code: str) -> None:
        owner = self.agent.storage.use_telegram_code(code)
        if owner is None:
            await self.bot.send(chat_id, "❌ Код невірний або прострочений. Отримай новий: kru telegram link")
            return
        self.agent.storage.set_telegram_chat(owner, chat_id)
        await self.bot.send(chat_id, f"✅ {self.config.agent_name} підключено для «{owner}»! Пиши задачу — я відповім тут.\n\n"
                                     + HELP.format(name=self.config.agent_name))

    # ---------- кнопки дозволу ----------
    async def _on_callback(self, cq: dict[str, Any]) -> None:
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        owner = self.agent.storage.telegram_owner(chat_id)
        m = re.fullmatch(r"c:(\w+):([ynt])", str(cq.get("data") or ""))
        if owner is None or not m:
            await self.bot.answer_callback(cq["id"])
            return
        cid, choice = m.groups()
        rt = self.hub.get(owner)
        if rt is None or cid not in rt.pending:
            await self.bot.answer_callback(cq["id"], "Цей запит уже неактуальний")
            return
        rt.answer(cid, choice != "n", "task" if choice == "t" else "")
        verdict = {"y": "✅ Дозволено", "n": "❌ Відхилено", "t": "✅ Дозволено до кінця задачі"}[choice]
        await self.bot.answer_callback(cq["id"], verdict)
        try:
            await self.bot.edit(chat_id, msg["message_id"], f"{msg.get('text', '')}\n\n{verdict}")
        except TelegramError:
            pass
