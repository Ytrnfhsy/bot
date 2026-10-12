"""Командний рядок: kru serve | chat | ask | health."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

from .config import Config

DIM, BOLD, CYAN, YELLOW, GREEN, RED, RESET = (
    ("\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[32m", "\033[31m", "\033[0m")
    if sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    else ("",) * 7
)

_current: asyncio.Task | None = None  # задача, яку скасовує Ctrl+C

HELP = (
    f"{DIM}Усі чати зберігаються автоматично. Команди: /new — новий чат, /chats — список чатів,\n"
    f"/open N — відкрити чат №N зі списку, /rename назва — перейменувати поточний, /delete N — видалити,\n"
    f"/export [файл.md] — експорт у Markdown, /exit — вийти, Ctrl+C — скасувати поточну задачу{RESET}"
)


def new_session_id() -> str:
    return "cli-" + time.strftime("%Y%m%d-%H%M%S")


def parse_chat_command(text: str, printer: "Printer") -> tuple[dict[str, Any], str | None] | None:
    """/chats, /open, /rename, /delete, /export → (повідомлення протоколу, шлях для експорту).

    N — номер зі списку /chats або сам id чату.
    """
    cmd, _, arg = text.partition(" ")
    arg = arg.strip()

    def chat_id(ref: str) -> str:
        if ref.isdigit() and 1 <= int(ref) <= len(printer.listed):
            return printer.listed[int(ref) - 1]
        return ref

    if cmd == "/chats":
        return {"type": "chat_list"}, None
    if cmd in {"/open", "/delete"} and arg:
        return {"type": "chat_open" if cmd == "/open" else "chat_delete", "id": chat_id(arg)}, None
    if cmd in {"/rename", "/save"} and arg:
        return {"type": "chat_rename", "id": printer.session, "title": arg}, None
    if cmd == "/export":
        return {"type": "chat_export", "id": printer.session}, arg or None
    return None


def run_chat_command_locally(agent: Any, msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Те саме, що робить сервер на chat_* повідомлення, але без мережі (для локального режиму)."""
    kind, cid = msg["type"], str(msg.get("id") or "")
    try:
        events: list[dict[str, Any]] = []
        if kind == "chat_rename":
            events.append({"type": "chat_renamed", **agent.rename_chat(cid, msg.get("title"))})
        elif kind == "chat_open":
            events.append({"type": "chat_opened", **agent.open_chat(cid)})
        elif kind == "chat_delete":
            agent.delete_chat(cid)
        elif kind == "chat_export":
            events.append({"type": "chat_export", **agent.export_chat(cid)})
        if kind in {"chat_list", "chat_delete"}:
            events.append({"type": "chat_list", "chats": agent.list_chats()})
        return events
    except ValueError as e:
        return [{"type": "chat_error", "message": str(e)}]


class Printer:
    """Друкує події агента в термінал."""

    def __init__(self, show_thinking: bool, session: str) -> None:
        self.session = session  # поточний чат
        self.listed: list[str] = []  # id чатів з останнього /chats (для /open N)
        self.export_path: str | None = None  # куди записати наступний chat_export
        self.show_thinking = show_thinking
        self.streaming = False
        self.in_thinking = False

    def _end_stream(self) -> None:
        if self.streaming or self.in_thinking:
            print(RESET)
            self.streaming = self.in_thinking = False

    def __call__(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "thinking" and self.show_thinking:
            if not self.in_thinking:
                print(f"{DIM}💭 ", end="")
                self.in_thinking = True
            print(ev["text"], end="", flush=True)
        elif t == "token":
            if self.in_thinking:
                self._end_stream()
            if not self.streaming:
                print(f"{CYAN}", end="")
                self.streaming = True
            print(ev["text"], end="", flush=True)
        elif t == "tool_call":
            self._end_stream()
            args = json.dumps(ev.get("args", {}), ensure_ascii=False)
            print(f"{YELLOW}🔧 {ev['name']}{RESET} {DIM}{args[:200]}{RESET}")
        elif t == "tool_result":
            mark = f"{GREEN}✓" if ev.get("ok") else f"{RED}✗"
            preview = " ".join(str(ev.get("preview", "")).split())[:160]
            print(f"   {mark}{RESET} {DIM}{preview}{RESET}")
        elif t == "final":
            was_streaming = self.streaming
            self._end_stream()
            if not was_streaming:
                print(f"\n{BOLD}🤖{RESET} {ev['text']}\n")
            else:
                print()
        elif t == "error":
            self._end_stream()
            print(f"{RED}Помилка: {ev['message']}{RESET}")
        elif t == "hello":
            eye = {True: " · 👁 зір увімкнено", False: " · без зору"}.get(ev.get("vision"), "")
            who = f" · користувач {ev['user']}" if ev.get("user") else ""
            print(f"{DIM}Підключено. Модель: {ev.get('model')}{eye}{who}{RESET}")
        elif t == "vision":
            if ev.get("state") == "attached":
                print(f"{DIM}👁 зображення: {', '.join(ev.get('images', []))}{RESET}")
            else:
                print(f"{YELLOW}⚠ Модель зараз не бачить зображень: {ev.get('reason', '')}{RESET}")
        elif t == "chat_renamed":
            print(f"{GREEN}✏️ Назва: «{ev['title']}»{RESET}")
        elif t == "chat_list":
            self.listed = [c["id"] for c in ev["chats"]]
            if not ev["chats"]:
                print(f"{DIM}Чатів ще немає{RESET}")
            for n, c in enumerate(ev["chats"], 1):
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["updated_at"]))
                mark = " ◀ поточний" if c["id"] == self.session else ""
                print(f"  {BOLD}{n}.{RESET} {c['title']} {DIM}· {c['count']} повідомл. · {when}{mark}{RESET}")
        elif t == "chat_opened":
            self.session = ev["id"]
            print(f"{GREEN}📂 Відкрито «{ev['title']}» — можна продовжувати розмову{RESET}")
            for item in ev["transcript"][-6:]:
                who = {"user": "ти", "assistant": "🤖", "tool": "  "}[item["role"]]
                print(f"{DIM}{who}: {' '.join(item['text'].split())[:150]}{RESET}")
        elif t == "chat_export":
            path = self.export_path or f"{ev['title'][:40].strip() or 'chat'}.md".replace("/", "_")
            self.export_path = None
            with open(path, "w", encoding="utf-8") as f:
                f.write(ev["markdown"])
            print(f"{GREEN}📤 Експортовано у {os.path.abspath(path)}{RESET}")
        elif t == "chat_error":
            print(f"{YELLOW}{ev['message']}{RESET}")


def ask_yes_no(summary: str) -> bool:
    print(f"\n{YELLOW}⚠ Агент хоче виконати дію:{RESET}\n{summary}\n")
    try:
        answer = input(f"{BOLD}Дозволити? [y/N]: {RESET}").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes", "т", "так", "д"}


# ---------------- локальний режим ----------------
async def _local_chat(cfg: Config, args: argparse.Namespace, once: str | None) -> None:
    from .agent import Agent

    agent = Agent(cfg)
    printer = Printer(args.thinking, args.session or new_session_id())

    async def emit(ev: dict[str, Any]) -> None:
        printer(ev)

    async def confirm(tool: str, summary: str, _args: dict[str, Any]) -> bool:
        printer._end_stream()
        return await asyncio.to_thread(ask_yes_no, summary)

    health = await agent.llm.health()
    if not health.get("ok"):
        print(f"{RED}LLM-сервер недоступний: {health.get('error')}{RESET}")
    elif not health.get("model_present"):
        hint = "перевір --alias у llama-server" if cfg.backend == "openai" else f"виконай: ollama pull {cfg.model}"
        print(f"{YELLOW}Модель {cfg.model} не знайдено — {hint}{RESET}")

    try:
        if once:
            await agent.run(printer.session, once, emit, confirm)
            return
        print(f"{BOLD}Kru{RESET} — модель {cfg.model}, workspace {cfg.workspace}\n{HELP}")
        while True:
            try:
                text = (await asyncio.to_thread(input, f"{BOLD}ти › {RESET}")).strip()
            except EOFError:
                break
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                break
            if text in {"/new", "/reset"}:
                printer.session = new_session_id()
                print(f"{DIM}Новий чат (попередній збережено){RESET}")
                continue
            if parsed := parse_chat_command(text, printer):
                msg, printer.export_path = parsed
                for ev in run_chat_command_locally(agent, msg):
                    printer(ev)
                continue
            global _current
            _current = asyncio.create_task(agent.run(printer.session, text, emit, confirm))
            try:
                await asyncio.wait({_current})
                _current.result()
            except asyncio.CancelledError:
                print(f"\n{DIM}⏹ скасовано{RESET}")
            except Exception as e:  # noqa: BLE001
                print(f"{RED}Помилка: {e}{RESET}")
    finally:
        await agent.close()


# ---------------- віддалений режим ----------------
async def _remote_chat(url: str, token: str, args: argparse.Namespace, once: str | None) -> None:
    import websockets

    try:
        await _remote_session(url, token, args, once)
    except websockets.ConnectionClosed as e:
        code = e.rcvd.code if e.rcvd else None
        reason = "невірний токен (перевір --token / KRU_TOKEN)" if code == 4401 else f"з'єднання закрито ({e})"
        print(f"{RED}Сервер відхилив підключення: {reason}{RESET}")
    except websockets.InvalidStatus as e:
        hint = " — адреса має закінчуватися на /ws" if e.response.status_code in (403, 404) else ""
        print(f"{RED}Сервер відповів HTTP {e.response.status_code}{hint}{RESET}")
    except (OSError, asyncio.TimeoutError, websockets.InvalidURI, websockets.InvalidHandshake) as e:
        print(f"{RED}Не вдалося підключитися до {url}: {e or type(e).__name__}{RESET}")


async def _remote_session(url: str, token: str, args: argparse.Namespace, once: str | None) -> None:
    import websockets

    if "://" not in url:
        url = "ws://" + url
    if url.rstrip("/").count("/") == 2:  # ws://host:port без шляху
        url = url.rstrip("/") + "/ws"
    if token:
        url += ("&" if "?" in url else "?") + f"token={token}"
    printer = Printer(args.thinking, args.session or new_session_id())
    async with websockets.connect(url, max_size=None, ping_interval=20, open_timeout=15) as ws:
        done = asyncio.Event()
        # перше повідомлення — hello; при невірному токені сервер тут закриє з'єднання (4401)
        printer(json.loads(await asyncio.wait_for(ws.recv(), 15)))

        async def reader() -> None:
            try:
                async for raw in ws:
                    ev = json.loads(raw)
                    if ev.get("type") == "confirm_request":
                        printer._end_stream()
                        ok = await asyncio.to_thread(ask_yes_no, ev["summary"])
                        await ws.send(json.dumps({"type": "confirm_response", "id": ev["id"], "approved": ok}))
                        continue
                    printer(ev)
                    if ev.get("type") == "status" and ev.get("state") == "idle":
                        done.set()
                    elif ev.get("type") == "error":
                        done.set()
            finally:
                done.set()  # з'єднання обірвалось — не чекаємо вічно

        reader_task = asyncio.create_task(reader())
        try:
            async def run_one(text: str) -> None:
                global _current
                done.clear()
                await ws.send(json.dumps({"type": "task", "text": text, "session_id": printer.session}))
                _current = asyncio.create_task(done.wait())
                await asyncio.wait({_current})
                if _current.cancelled():
                    await ws.send(json.dumps({"type": "cancel"}))
                    raise asyncio.CancelledError
                if reader_task.done():
                    reader_task.result()  # прокидаємо ConnectionClosed нагору
                    raise websockets.ConnectionClosedError(None, None)

            if once:
                await run_one(once)
                return
            print(f"{BOLD}Kru{RESET} (віддалено: {url.split('?')[0]})\n{HELP}")
            while True:
                try:
                    text = (await asyncio.to_thread(input, f"{BOLD}ти › {RESET}")).strip()
                except EOFError:
                    break
                if not text:
                    continue
                if text in {"/exit", "/quit"}:
                    break
                if text in {"/new", "/reset"}:
                    printer.session = new_session_id()
                    print(f"{DIM}Новий чат (попередній збережено){RESET}")
                    continue
                if parsed := parse_chat_command(text, printer):
                    msg, printer.export_path = parsed
                    await ws.send(json.dumps(msg, ensure_ascii=False))
                    await asyncio.sleep(0.3)  # відповідь надрукує reader
                    continue
                try:
                    await run_one(text)
                except asyncio.CancelledError:
                    print(f"\n{DIM}⏹ скасовано{RESET}")
        finally:
            reader_task.cancel()


def _run_interruptible(coro_factory) -> None:
    """Запускає REPL так, щоб Ctrl+C скасовував поточну задачу, а не весь процес."""
    loop = asyncio.new_event_loop()
    main = loop.create_task(coro_factory())

    import signal

    def on_sigint() -> None:
        if _current is not None and not _current.done():
            _current.cancel()
        else:
            print()
            main.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
    except (NotImplementedError, RuntimeError):
        pass
    try:
        loop.run_until_complete(main)
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


def _user_command(cfg: Config, args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    from .users import OWNER, UserStore

    store = UserStore(cfg)
    if args.user_cmd == "add":
        try:
            token = store.add(args.name, "admin" if args.admin else "user")
        except ValueError as e:
            raise SystemExit(f"{RED}{e}{RESET}") from None
        name = args.name.strip().lower()
        print(f"{GREEN}Користувач {name} готовий ({'admin' if args.admin else 'user'}).{RESET}")
        print(f"Токен (показується лише зараз — збережи його):\n\n  {BOLD}{token}{RESET}\n")
        print(f"{DIM}Вкажи його в налаштуваннях робота (поле «Токен») на будь-якому ПК користувача.{RESET}")
        if not cfg.token:
            print(f"{YELLOW}KRU_TOKEN не задано: тепер без токена сервер не пускає. "
                  f"Власнику ({OWNER}) задай KRU_TOKEN або створи собі користувача з --admin.{RESET}")
    elif args.user_cmd == "list":
        if cfg.token:
            print(f"  {BOLD}{OWNER}{RESET} · admin · токен із KRU_TOKEN")
        for u in store.list():
            when = time.strftime("%Y-%m-%d", time.localtime(u.get("created_at", 0)))
            print(f"  {BOLD}{u['name']}{RESET} · {u.get('role', 'user')} · з {when}")
        if not cfg.token and not store.list():
            print(f"{DIM}Користувачів немає — сервер працює для одного власника без токена.{RESET}")
    elif args.user_cmd == "revoke":
        ok = store.revoke(args.name)
        print(f"{GREEN}Користувача {args.name} видалено{RESET}" if ok else f"{YELLOW}Немає користувача {args.name}{RESET}")
        if ok:
            print(f"{DIM}Його чати й пам'ять лишаються в базі; тека — {cfg.users_workspace / args.name}{RESET}")
    else:
        parser.print_help()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kru", description="Kru — агент на Qwen3.6 з інструментами")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="запустити сервер (WebSocket/HTTP)")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)

    for name, help_ in (("chat", "інтерактивний чат (REPL)"), ("ask", "одноразова задача")):
        p = sub.add_parser(name, help=help_)
        if name == "ask":
            p.add_argument("text", nargs="+", help="текст задачі")
        p.add_argument("--server", help="ws://host:8765/ws — підключитися до віддаленого сервера")
        p.add_argument("--token", default=os.environ.get("KRU_TOKEN", os.environ.get("BONSAI_TOKEN", "")))
        p.add_argument("--session", help="id чату, щоб продовжити його (типово — новий чат)")
        p.add_argument("--thinking", action="store_true", help="показувати міркування моделі")
        p.add_argument("--yes", action="store_true", help="авто-підтвердження небезпечних дій (лише локально)")

    sub.add_parser("health", help="перевірити, чи доступна модель")

    p_user = sub.add_parser("user", help="користувачі: у кожного свої чати, пам'ять і тека")
    user_sub = p_user.add_subparsers(dest="user_cmd")
    p_add = user_sub.add_parser("add", help="створити користувача або видати йому новий токен")
    p_add.add_argument("name")
    p_add.add_argument("--admin", action="store_true", help="повний доступ (shell, Python, усі файли)")
    user_sub.add_parser("list", help="список користувачів")
    p_rev = user_sub.add_parser("revoke", help="видалити користувача (токен перестане працювати)")
    p_rev.add_argument("name")

    args = parser.parse_args(argv)
    if args.cmd == "user":
        # ті самі налаштування, що й у сервісу (DATA_DIR, KRU_TOKEN)
        from .config import _load_dotenv
        from pathlib import Path

        _load_dotenv(Path.home() / ".config" / "bonsai-agent.env")
    cfg = Config.from_env()

    if args.cmd == "serve":
        if args.host:
            cfg.host = args.host
        if args.port:
            cfg.port = args.port
        from .server import run_server

        run_server(cfg)
    elif args.cmd in {"chat", "ask"}:
        once = " ".join(args.text) if args.cmd == "ask" else None
        if args.yes:
            cfg.auto_approve = True
        if args.server:
            _run_interruptible(lambda: _remote_chat(args.server, args.token, args, once))
        else:
            _run_interruptible(lambda: _local_chat(cfg, args, once))
    elif args.cmd == "user":
        _user_command(cfg, args, p_user)
    elif args.cmd == "health":
        from .llm import create_llm

        async def check() -> None:
            c = create_llm(cfg)
            print(json.dumps(await c.health(), ensure_ascii=False, indent=2))
            await c.close()

        asyncio.run(check())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
