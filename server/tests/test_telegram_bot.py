"""Telegram у дві сторони: повідомлення боту → агент → відповідь; дозволи кнопками; прив'язка кодом."""

from __future__ import annotations

import asyncio

from kru_agent.agent import Agent
from kru_agent.server import TaskHub
from kru_agent.telegram_bot import new_code, TelegramGateway
from kru_agent.users import UserStore

from .conftest import FakeLLM, call, text

CHAT = "555"


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, list | None]] = []
        self.edits: list[tuple[str, int, str]] = []
        self.deleted: list[int] = []
        self.callbacks: list[str] = []
        self.files: dict[str, tuple[bytes, str]] = {}
        self._id = 100

    async def send(self, chat_id, text, buttons=None, markdown=False):
        self._id += 1
        self.sent.append((str(chat_id), text, buttons))
        return self._id

    async def edit(self, chat_id, message_id, text):
        self.edits.append((str(chat_id), message_id, text))

    async def delete(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def typing(self, chat_id):
        pass

    async def answer_callback(self, callback_id, text=""):
        self.callbacks.append(text)

    async def download(self, file_id, max_bytes=0):
        return self.files[file_id]

    def texts(self):
        return [t for _, t, _ in self.sent]


def make(cfg, script, linked=True):
    agent = Agent(cfg, llm=FakeLLM(script))
    bot = FakeBot()
    gw = TelegramGateway(agent, TaskHub(), UserStore(cfg), bot=bot)
    if linked:
        agent.storage.set_telegram_chat("owner", CHAT)
    return gw, agent, bot


def msg(text="", chat=CHAT, **extra):
    return {"update_id": 1, "message": {"chat": {"id": int(chat)}, "text": text, **extra}}


async def test_unlinked_chat_is_refused(cfg):
    gw, agent, bot = make(cfg, [], linked=False)
    await gw.handle(msg("привіт", chat="999"))
    assert "приватний бот" in bot.texts()[0]
    assert agent.llm.calls == []


async def test_link_by_one_time_code(cfg):
    gw, agent, bot = make(cfg, [], linked=False)
    agent.storage.add_telegram_code("owner", "123456")
    await gw.handle(msg("/link 000000", chat="777"))
    assert "невірний" in bot.texts()[-1]
    await gw.handle(msg("/link 123456", chat="777"))
    assert agent.storage.telegram_owner("777") == "owner" and "підключено" in bot.texts()[-1]
    await gw.handle(msg("/link 123456", chat="888"))  # код одноразовий
    assert agent.storage.telegram_owner("888") is None
    # перебір кодів: після 5 невірних чат блокується, навіть правильний код не спрацює
    code = new_code()
    assert len(code) == 10 and code.isalnum()
    agent.storage.add_telegram_code("owner", code)
    for _ in range(5):
        await gw.handle(msg("/link AAAA2222BB", chat="999"))
    assert "Забагато спроб" in bot.texts()[-1]
    await gw.handle(msg(f"/link {code.lower()}", chat="999"))
    assert agent.storage.telegram_owner("999") is None
    await gw.handle(msg(f"/link {code[:5]}-{code[5:].lower()}", chat="555"))  # як надрукував — регістр і дефіс неважливі
    assert agent.storage.telegram_owner("555") == "owner"


async def test_message_runs_agent_and_replies(cfg):
    gw, agent, bot = make(cfg, [call("current_datetime"), text("Зараз 17:20")])
    await gw.handle(msg("котра година?"))
    assert bot.texts()[-1] == "Зараз 17:20"
    assert any(t.startswith("⏳ ") for t in bot.texts())  # повідомлення з ходом роботи…
    assert bot.deleted  # …прибирається, коли готова відповідь
    sid = agent.storage.telegram_session("owner")
    chat = agent.storage.load_chat(sid)
    assert chat["title"].startswith("✈️ ") and [m["role"] for m in chat["messages"]][0] == "user"
    assert gw.hub.get("owner") is None


async def test_new_chat_and_help(cfg):
    gw, agent, bot = make(cfg, [text("1"), text("2")])
    await gw.handle(msg("/start"))
    assert "/new" in bot.texts()[-1]
    await gw.handle(msg("перше"))
    first = agent.storage.telegram_session("owner")
    await gw.handle(msg("/new"))
    await gw.handle(msg("друге"))
    assert agent.storage.telegram_session("owner") != first
    assert len(agent.list_chats("owner")) == 2


async def test_permission_buttons(cfg):
    gw, agent, bot = make(cfg, [
        call("write_file", path="a.txt", content="1"), call("write_file", path="b.txt", content="2"), text("Готово"),
    ])
    job = asyncio.create_task(gw.handle(msg("створи два файли")))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if any(b for _, _, b in bot.sent):
            break
    _, ask, buttons = next(s for s in bot.sent if s[2])
    assert "Агент просить дозволу" in ask and "a.txt" in ask
    labels = [label for row in buttons for label, _ in row]
    assert labels == ["✅ Дозволити", "❌ Відхилити", "✅ До кінця задачі"]
    data = buttons[1][0][1]  # «До кінця задачі»
    await gw.handle({"update_id": 2, "callback_query": {
        "id": "cb1", "data": data, "message": {"chat": {"id": int(CHAT)}, "message_id": 7, "text": ask}}})
    await asyncio.wait_for(job, 5)
    assert bot.callbacks == ["✅ Дозволено до кінця задачі"]
    assert sum(1 for _, _, b in bot.sent if b) == 1  # другий файл — уже без запиту
    assert (cfg.workspace / "b.txt").read_text() == "2" and bot.texts()[-1] == "Готово"
    # стара кнопка після завершення задачі — нічого не ламає
    await gw.handle({"update_id": 3, "callback_query": {
        "id": "cb2", "data": data, "message": {"chat": {"id": int(CHAT)}, "message_id": 7, "text": ask}}})
    assert bot.callbacks[-1] == "Цей запит уже неактуальний"


async def test_busy_and_stop(cfg):
    gw, agent, bot = make(cfg, [call("run_shell", command="sleep 100"), text("не дійде")])
    job = asyncio.create_task(gw.handle(msg("довга задача")))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if gw.hub.get("owner"):
            break
    await gw.handle(msg("ще одна"))
    assert "Зараз виконую" in bot.texts()[-1]
    await gw.handle(msg("/stop"))
    await asyncio.wait_for(job, 5)
    assert "зупинено" in bot.texts()[-1] and gw.hub.get("owner") is None


async def test_photo_is_saved_and_shown_to_model(cfg):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (0, 128, 255)).save(buf, "JPEG")
    gw, agent, bot = make(cfg, [text("Синій прямокутник")])
    bot.files["F1"] = (buf.getvalue(), "photos/file_1.jpg")
    await gw.handle(msg(caption="що тут?", photo=[{"file_id": "F0"}, {"file_id": "F1"}]))
    saved = list((cfg.workspace / "uploads").glob("tg-*file_1.jpg"))
    assert len(saved) == 1
    user_msg = agent.llm.calls[0]["messages"][-1]
    assert user_msg["content"].endswith(f"uploads/{saved[0].name}") or f"uploads/{saved[0].name}" in user_msg["content"]
    assert user_msg.get("images") and bot.texts()[-1] == "Синій прямокутник"
