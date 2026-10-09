"""Командний рядок: bonsai serve | chat | ask | health."""

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
    f"{DIM}Команди: /reset — нова розмова, /save [назва] — зберегти чат, /chats — збережені чати,\n"
    f"/open N — відкрити чат №N, /delete N — видалити, /export [файл.md] — експорт у Markdown,\n"
    f"/exit — вийти, Ctrl+C — скасувати поточну задачу{RESET}"
)


def parse_chat_command(text: str, session: str) -> tuple[dict[str, Any], str | None] | None:
    """/save, /chats, /open, /delete, /export → (повідомлення протоколу, шлях для експорту)."""
    cmd, _, arg = text.partition(" ")
    arg = arg.strip()
    if cmd == "/save":
        return {"type": "chat_save", "session_id": session, "title": arg or None}, None
    if cmd == "/chats":
        return {"type": "chat_list"}, None
    if cmd in {"/open", "/delete"} and arg.isdigit():
        kind = "chat_open" if cmd == "/open" else "chat_delete"
        return {"type": kind, "id": int(arg), "session_id": session}, None
    if cmd == "/export":
        return {"type": "chat_export", "session_id": session}, arg or None
    return None


def run_chat_command_locally(agent: Any, msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Те саме, що робить сервер на chat_* повідомлення, але без мережі (для локального режиму)."""
    kind, session, cid = msg["type"], msg.get("session_id", "cli"), msg.get("id")
    try:
        events: list[dict[str, Any]] = []
        if kind == "chat_save":
            events.append({"type": "chat_saved", **agent.save_chat(session, msg.get("title"))})
        elif kind == "chat_open":
            events.append({"type": "chat_opened", **agent.open_chat(cid, session)})
        elif kind == "chat_delete":
            agent.delete_chat(cid)
        elif kind == "chat_export":
            events.append({"type": "chat_export", **agent.export_chat(session)})
        if kind in {"chat_list", "chat_delete"}:
            events.append({"type": "chat_list", "chats": agent.list_chats()})
        return events
    except ValueError as e:
        return [{"type": "chat_error", "message": str(e)}]


class Printer:
    """Друкує події агента в термінал."""

    def __init__(self, show_thinking: bool) -> None:
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
            print(f"{DIM}Підключено. Модель: {ev.get('model')}{RESET}")
        elif t == "chat_saved":
            print(f"{GREEN}💾 Збережено як #{ev['id']} «{ev['title']}»{RESET}")
        elif t == "chat_list":
            if not ev["chats"]:
                print(f"{DIM}Збережених чатів немає{RESET}")
            for c in ev["chats"]:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["updated_at"]))
                print(f"  {BOLD}#{c['id']}{RESET} {c['title']} {DIM}· {c['count']} повідомл. · {when}{RESET}")
        elif t == "chat_opened":
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
    printer = Printer(args.thinking)

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
            await agent.run(args.session, once, emit, confirm)
            return
        print(f"{BOLD}Bonsai Agent{RESET} — модель {cfg.model}, workspace {cfg.workspace}\n{HELP}")
        while True:
            try:
                text = (await asyncio.to_thread(input, f"{BOLD}ти › {RESET}")).strip()
            except EOFError:
                break
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                break
            if text == "/reset":
                agent.reset_session(args.session)
                print(f"{DIM}Розмову очищено{RESET}")
                continue
            if parsed := parse_chat_command(text, args.session):
                msg, printer.export_path = parsed
                for ev in run_chat_command_locally(agent, msg):
                    printer(ev)
                continue
            global _current
            _current = asyncio.create_task(agent.run(args.session, text, emit, confirm))
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
        reason = "невірний токен (перевір --token / BONSAI_TOKEN)" if code == 4401 else f"з'єднання закрито ({e})"
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
    printer = Printer(args.thinking)
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
                await ws.send(json.dumps({"type": "task", "text": text, "session_id": args.session}))
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
            print(f"{BOLD}Bonsai Agent{RESET} (віддалено: {url.split('?')[0]})\n{HELP}")
            while True:
                try:
                    text = (await asyncio.to_thread(input, f"{BOLD}ти › {RESET}")).strip()
                except EOFError:
                    break
                if not text:
                    continue
                if text in {"/exit", "/quit"}:
                    break
                if text == "/reset":
                    await ws.send(json.dumps({"type": "reset", "session_id": args.session}))
                    continue
                if parsed := parse_chat_command(text, args.session):
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bonsai", description="Bonsai Agent — агент на базі Ollama")
    sub = parser.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="запустити сервер (WebSocket/HTTP)")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)

    for name, help_ in (("chat", "інтерактивний чат (REPL)"), ("ask", "одноразова задача")):
        p = sub.add_parser(name, help=help_)
        if name == "ask":
            p.add_argument("text", nargs="+", help="текст задачі")
        p.add_argument("--server", help="ws://host:8765/ws — підключитися до віддаленого сервера")
        p.add_argument("--token", default=os.environ.get("BONSAI_TOKEN", ""))
        p.add_argument("--session", default="cli")
        p.add_argument("--thinking", action="store_true", help="показувати міркування моделі")
        p.add_argument("--yes", action="store_true", help="авто-підтвердження небезпечних дій (лише локально)")

    sub.add_parser("health", help="перевірити Ollama і модель")

    args = parser.parse_args(argv)
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
