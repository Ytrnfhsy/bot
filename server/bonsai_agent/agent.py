"""Цикл агента: LLM ↔ інструменти, з підтвердженням небезпечних дій і пам'яттю."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

from .config import Config
from .images import find_image_refs, is_image, load_image
from .llm import ChatResult, OllamaClient, OpenAICompatClient, ToolsNotSupported, VisionNotSupported, create_llm
from .storage import Storage
from .tools import ToolContext, ToolError, ToolRegistry, build_registry, truncate
from .tools.memory import format_memories
from .users import ADMIN_ONLY_TOOLS, OWNER, User, workspace_for

log = logging.getLogger("kru.agent")

EmitFunc = Callable[[dict[str, Any]], Awaitable[None]]
ConfirmFunc = Callable[[str, str, dict[str, Any]], Awaitable[bool]]  # (tool, summary, args) -> approved

SYSTEM_PROMPT = """Your name is {name}. You are a capable autonomous assistant agent running on the user's server.
When asked who you are, introduce yourself as {name}.{persona}
You solve tasks step by step using tools, and you finish with a clear, well-structured answer.

Current date/time: {now}
Workspace directory (files, shell and python run here): {workspace}
Always answer in the user's language (default: {language}).

How to work:
- Think about what is needed, then act. Use tools whenever they help; do not invent facts.
- For ANY factual question about the world, recent events, prices, people, docs, etc.: use web_search,
  then open 1-3 of the best sources with fetch_url to verify, and cite source URLs in the final answer.
  If results are poor, reformulate (synonyms, English query, kind="news"). Cross-check important facts.
- Use run_python for calculations and data processing instead of computing in your head.
- Before editing a file, read it. Prefer edit_file for small changes.
- run_shell, run_python, write_file and edit_file require user approval; if the user declines, adapt or ask.
- Save durable useful facts about the user or their projects with `remember`; use `recall` when past context may help.
- When the task is done, reply with the final answer only (no tool call). Be concise but complete; use Markdown.
{vision}{memories}"""

JSON_PROTOCOL = """
TOOLS — you call them by replying with ONLY a JSON object in a ```json code block, exactly one per message:
```json
{{"tool": "<tool name>", "args": {{...}}}}
```
After each call you receive the result in a message starting with "TOOL RESULT". Then continue.
When you have the final answer, reply with plain text (no JSON tool block).

Available tools:
{catalog}
"""

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def strip_think(text: str) -> str:
    text = _THINK_RE.sub("", text)
    # незакритий <think> на початку (обрізаний стрім)
    if text.lstrip().lower().startswith("<think>"):
        return ""
    return text.strip()


def _first_json_object(text: str) -> str | None:
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        start = text.find("{", start + 1)
    return None


def parse_json_tool_call(text: str) -> tuple[str, dict[str, Any]] | None:
    """Шукає у тексті виклик інструмента формату {"tool": ..., "args": {...}}."""
    text = strip_think(text)
    candidates = _FENCE_RE.findall(text)
    obj = _first_json_object(text)
    if obj:
        candidates.append(obj)
    for cand in candidates:
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        if isinstance(data, dict):
            name = data.get("tool") or data.get("name") or data.get("function")
            args = data.get("args", data.get("arguments", data.get("parameters", {})))
            if isinstance(name, str) and name:
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                return name, args if isinstance(args, dict) else {}
    return None


def _normalize_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


class Agent:
    def __init__(
        self,
        config: Config,
        llm: OllamaClient | OpenAICompatClient | None = None,
        storage: Storage | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.config = config
        config.ensure_dirs()
        self.llm = llm or create_llm(config)
        self.storage = storage or Storage(config.db_path)
        self.registry = registry or build_registry()
        self.native_tools = config.tool_mode != "json"
        self.vision = config.vision != "off"
        self.vision_reason = ""  # чому зору немає (для користувача)
        self._vision_checked = 0.0
        self._session_locks: dict[str, asyncio.Lock] = {}

    def make_context(self, user: User | None = None) -> ToolContext:
        """Контекст задачі конкретного користувача: його тека, пам'ять і дозволені інструменти."""
        user = user or User(OWNER, "admin")
        ws = workspace_for(self.config, user)
        ws.mkdir(parents=True, exist_ok=True)
        return ToolContext(
            config=self.config,
            storage=self.storage,
            owner=user.name,
            workspace=ws,
            allow_outside=self.config.allow_outside_workspace and user.is_admin,
            denied_tools=frozenset() if user.is_admin else ADMIN_ONLY_TOOLS,
            vision_ok=self.vision,
        )

    # ---------------- побудова контексту ----------------
    def _system_prompt(self, user_text: str, ctx: ToolContext) -> str:
        memories = ""
        try:
            items = self.storage.search_memories(user_text, limit=6, owner=ctx.owner)
        except Exception:  # noqa: BLE001
            items = []
        if items:
            memories = "\nRelevant long-term memories (may be useful):\n" + format_memories(items) + "\n"
        prompt = SYSTEM_PROMPT.format(
            now=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z (%A)"),
            workspace=ctx.workspace,
            language=self.config.language,
            name=self.config.agent_name,
            persona=f"\nPersonality: {self.config.persona}" if self.config.persona else "",
            vision=(
                "- You can SEE images (you are a vision-language model). Pictures attached to a message are already "
                "in front of you: look at them and answer directly — read text on screenshots yourself, describe what "
                "you see. NEVER try to open, OCR or analyse an image with run_python/run_shell, and never install "
                "libraries (PIL, pytesseract, opencv…) for that. To look at an image file in the workspace that is not "
                "attached (e.g. one you found or created), call view_image.\n"
                if self.vision
                else "- You currently CANNOT see images. If the user asks about a picture, say that vision is turned off "
                "on the server; do not try to read images with scripts or OCR.\n"
            ),
            memories=memories,
        )
        if not self.native_tools:
            prompt += JSON_PROTOCOL.format(catalog=self.registry.text_catalog(ctx.denied_tools))
        return prompt

    def _trim_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def size(m: dict[str, Any]) -> int:
            return (
                len(m.get("content") or "")
                + len(json.dumps(m.get("tool_calls", ""), ensure_ascii=False))
                + 2500 * len(m.get("images") or [])  # картинка ≈ кілька тисяч токенів
            )

        msgs = list(history)
        total = sum(size(m) for m in msgs)
        while msgs and total > self.config.max_history_chars and len(msgs) > 1:
            total -= size(msgs.pop(0))
        # контекст має починатися з повідомлення користувача
        while msgs and msgs[0].get("role") != "user":
            msgs.pop(0)
        return msgs

    def _to_json_protocol(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Конвертує нативну історію (tool_calls / role=tool) у текстовий JSON-протокол."""
        out = []
        for m in history:
            if m.get("role") == "tool":
                out.append({"role": "user", "content": f"TOOL RESULT ({m.get('tool_name', '?')}):\n{m.get('content', '')}"})
            elif m.get("role") == "assistant" and m.get("tool_calls"):
                parts = [m.get("content") or ""]
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    parts.append(
                        "```json\n"
                        + json.dumps({"tool": fn.get("name"), "args": _normalize_args(fn.get("arguments"))}, ensure_ascii=False)
                        + "\n```"
                    )
                out.append({"role": "assistant", "content": "\n".join(p for p in parts if p)})
            else:
                msg = {"role": m["role"], "content": m.get("content", "")}
                if m.get("images"):
                    msg["images"] = m["images"]
                out.append(msg)
        return out

    # ---------------- зображення ----------------
    def _image_refs(self, text: str, ctx: ToolContext) -> list[str]:
        """Наявні в workspace картинки, згадані в тексті задачі."""
        found = []
        for ref in find_image_refs(text):
            try:
                p = ctx.resolve_path(ref)
            except ToolError:
                continue
            if p.is_file() and is_image(p):
                found.append(ref)
        return found

    async def refresh_vision(self, force: bool = False) -> bool:
        """KRU_VISION=auto: питаємо сервер моделі, чи він бачить зображення (не частіше ніж раз на 30 с).

        Так зір вмикається сам, щойно модель перезапустили з mmproj, — без перезапуску агента.
        """
        if self.config.vision != "auto":
            return self.vision
        now = time.monotonic()
        if not force and now - self._vision_checked < 30:
            return self.vision
        self._vision_checked = now
        probe = getattr(self.llm, "supports_vision", None)
        if probe is None:
            return self.vision
        try:
            result = await probe()
        except Exception:  # noqa: BLE001 — сервер недоступний: лишаємо як було
            return self.vision
        if result is not None:
            if result != self.vision:
                log.info("Зір %s (за даними сервера моделі)", "увімкнено" if result else "вимкнено")
            self.vision = result
            self.vision_reason = "" if result else "сервер моделі запущено без зору (немає mmproj)"
        return self.vision

    def _attach_images(self, msgs: list[dict[str, Any]], ctx: ToolContext) -> list[dict[str, Any]]:
        """Шляхи → base64 для останніх N повідомлень з картинками; решта — текстова примітка."""
        budget = self.config.max_images_in_context if self.vision else 0
        out: list[dict[str, Any]] = []
        for m in reversed(msgs):
            paths = m.get("images")
            if not paths:
                out.append(m)
                continue
            m = {k: v for k, v in m.items() if k != "images"}
            loaded, notes = [], []
            for path in paths:
                if budget <= 0:
                    why = (
                        "переглядалось раніше"
                        if self.vision
                        else "поточна модель не бачить зображень; скажи про це користувачу і не намагайся читати його скриптами"
                    )
                    notes.append(f"[зображення {path} — {why}]")
                    continue
                try:
                    loaded.append(load_image(ctx.resolve_path(path)))
                except Exception as e:  # noqa: BLE001
                    notes.append(f"[зображення {path} не вдалося відкрити: {e}]")
            if loaded:
                budget -= 1
                m["images"] = loaded
                shown = ", ".join(p for p in paths)
                m["content"] = (
                    f"[Прикріплене зображення: {shown}. Воно вже перед тобою — подивись і проаналізуй його сам, "
                    f"без скриптів, OCR чи встановлення бібліотек.]\n" + (m.get("content") or "")
                )
            if notes:
                m["content"] = (m.get("content") or "") + "\n" + "\n".join(notes)
            out.append(m)
        out.reverse()
        return out

    # ---------------- основний цикл ----------------
    async def run(
        self,
        session_id: str,
        user_text: str,
        emit: EmitFunc,
        confirm: ConfirmFunc,
        user: User | None = None,
    ) -> str:
        ctx = self.make_context(user)
        lock = self._session_locks.setdefault(f"{ctx.owner}:{session_id}", asyncio.Lock())
        async with lock:
            history = self.storage.load_session(session_id, ctx.owner)
            user_msg: dict[str, Any] = {"role": "user", "content": user_text}
            if refs := self._image_refs(user_text, ctx):
                await self.refresh_vision()
                ctx.vision_ok = self.vision
                user_msg["images"] = refs  # шляхи зберігаємо завжди — якщо зір з'явиться, картинку буде видно
                if self.vision:
                    await emit({"type": "vision", "state": "attached", "images": refs})
                else:
                    await emit({"type": "vision", "state": "unavailable", "images": refs,
                                "reason": self.vision_reason or "зір вимкнено (KRU_VISION=off)"})
            history.append(user_msg)

            def persist() -> None:
                # чат зберігається одразу й після кожного кроку — аварійне вимкнення не губить розмову
                self.storage.save_session(session_id, history, ctx.owner)

            persist()
            try:
                return await self._loop(history, user_text, emit, confirm, ctx, persist)
            finally:
                persist()

    async def _loop(
        self,
        history: list[dict[str, Any]],
        user_text: str,
        emit: EmitFunc,
        confirm: ConfirmFunc,
        ctx: ToolContext,
        persist: Callable[[], None] = lambda: None,
    ) -> str:
        for step in range(1, self.config.max_steps + 1):
            await emit({"type": "status", "state": "thinking", "step": step})
            result = await self._call_llm(history, user_text, emit, ctx)

            calls = self._extract_calls(result)
            if not calls:
                answer = strip_think(result.content) or "(порожня відповідь моделі)"
                history.append({"role": "assistant", "content": answer})
                await emit({"type": "final", "text": answer})
                await emit({"type": "status", "state": "idle"})
                return answer

            if self.native_tools:
                history.append(result.as_message())
            else:
                history.append({"role": "assistant", "content": result.content})

            ctx.pending_images.clear()
            for name, args in calls:
                output = await self._execute(name, args, emit, confirm, ctx)
                if self.native_tools:
                    history.append({"role": "tool", "tool_name": name, "content": output})
                else:
                    history.append({"role": "user", "content": f"TOOL RESULT ({name}):\n{output}"})
            # картинки від view_image — окремим повідомленням після всіх результатів інструментів
            if ctx.pending_images:
                paths = list(dict.fromkeys(ctx.pending_images))
                history.append({"role": "user", "content": "[view_image] " + ", ".join(paths), "images": paths})
                await emit({"type": "vision", "state": "attached", "images": paths})
            persist()

        answer = (
            f"Я зробив {self.config.max_steps} кроків і не встиг завершити задачу. "
            "Уточни, будь ласка, або попроси продовжити."
        )
        history.append({"role": "assistant", "content": answer})
        await emit({"type": "final", "text": answer})
        await emit({"type": "status", "state": "idle"})
        return answer

    async def _call_llm(
        self, history: list[dict[str, Any]], user_text: str, emit: EmitFunc, ctx: ToolContext
    ) -> ChatResult:
        async def on_token(kind: str, text: str) -> None:
            if kind == "thinking":
                await emit({"type": "thinking", "text": text})
            elif self.native_tools:
                await emit({"type": "token", "text": text})

        while True:
            trimmed = self._trim_history(history)
            msgs = trimmed if self.native_tools else self._to_json_protocol(trimmed)
            msgs = self._attach_images(msgs, ctx)
            messages = [{"role": "system", "content": self._system_prompt(user_text, ctx)}, *msgs]
            try:
                return await self.llm.chat(
                    messages,
                    tools=self.registry.schemas(ctx.denied_tools) if self.native_tools else None,
                    on_token=on_token,
                )
            except VisionNotSupported:
                if not self.vision:
                    raise
                log.warning("Модель не приймає зображення — вимикаю зір (додай mmproj, щоб увімкнути)")
                self.vision = ctx.vision_ok = False
                self.vision_reason = "модель відхилила зображення (немає mmproj?)"
                self._vision_checked = time.monotonic()  # перевіримо знову через 30 с, а не «назавжди»
                await emit({"type": "vision", "state": "unavailable", "images": [], "reason": self.vision_reason})
                continue
            except ToolsNotSupported:
                if self.config.tool_mode == "native":
                    raise
                log.warning("Модель не підтримує нативні tools — перемикаюсь на JSON-протокол")
                self.native_tools = False

    def _extract_calls(self, result: ChatResult) -> list[tuple[str, dict[str, Any]]]:
        if result.tool_calls:
            return [
                (tc.get("function", {}).get("name", ""), _normalize_args(tc.get("function", {}).get("arguments")))
                for tc in result.tool_calls
            ]
        # Навіть у нативному режимі деякі моделі пишуть виклик текстом.
        parsed = parse_json_tool_call(result.content)
        if parsed and self.registry.get(parsed[0]):
            if self.native_tools:
                # Перетворюємо на нативний виклик, щоб історія була узгодженою.
                result.tool_calls = [{"function": {"name": parsed[0], "arguments": parsed[1]}}]
                result.content = ""
            return [parsed]
        return []

    async def _execute(
        self, name: str, args: dict[str, Any], emit: EmitFunc, confirm: ConfirmFunc, ctx: ToolContext
    ) -> str:
        call_id = uuid.uuid4().hex[:8]
        tool = self.registry.get(name) if name not in ctx.denied_tools else None
        summary = tool.describe(args) if tool else f"{name}({args})"
        await emit({"type": "tool_call", "id": call_id, "name": name, "args": args, "summary": summary})

        if tool and tool.dangerous and not self.config.auto_approve:
            await emit({"type": "status", "state": "waiting_confirm"})
            approved = await confirm(name, summary, args)
            if not approved:
                msg = "Користувач ВІДХИЛИВ цю дію. Не повторюй її; запропонуй альтернативу або спитай, як діяти."
                await emit({"type": "tool_result", "id": call_id, "name": name, "ok": False, "preview": "відхилено"})
                return msg

        await emit({"type": "status", "state": "working", "tool": name})
        try:
            output = await self.registry.call(ctx, name, args)
            ok = True
        except ToolError as e:
            output, ok = f"ПОМИЛКА: {e}", False
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Інструмент %s впав", name)
            output, ok = f"ПОМИЛКА ({type(e).__name__}): {e}", False
        output = truncate(output, self.config.max_tool_output_chars)
        await emit({"type": "tool_result", "id": call_id, "name": name, "ok": ok, "preview": output[:500]})
        return output

    def reset_session(self, session_id: str, owner: str = OWNER) -> None:
        self.storage.clear_session(session_id, owner)

    # ---------------- чати (зберігаються автоматично) ----------------
    def list_chats(self, owner: str = OWNER) -> list[dict[str, Any]]:
        return self.storage.list_chats(owner)

    def open_chat(self, session_id: str, owner: str = OWNER) -> dict[str, Any]:
        """Чат для показу; клієнт далі просто пише в нього (session_id = id чату)."""
        from .chats import transcript

        chat = self.storage.load_chat(session_id, owner)
        if chat is None:
            raise ValueError("Чат не знайдено — можливо, його видалили")
        return {"id": chat["id"], "title": chat["title"], "transcript": transcript(chat["messages"])}

    def rename_chat(self, session_id: str, title: str, owner: str = OWNER) -> dict[str, Any]:
        title = " ".join(str(title or "").split())[:120]
        if not title:
            raise ValueError("Назва не може бути порожньою")
        if not self.storage.rename_chat(session_id, title, owner):
            raise ValueError("У цьому чаті ще немає повідомлень")
        return {"id": session_id, "title": title}

    def delete_chat(self, session_id: str, owner: str = OWNER) -> bool:
        return self.storage.clear_session(session_id, owner)

    def export_chat(self, session_id: str, owner: str = OWNER) -> dict[str, str]:
        from .chats import to_markdown

        chat = self.storage.load_chat(session_id, owner)
        if chat is None or not chat["messages"]:
            raise ValueError("Розмова порожня — нічого експортувати")
        title = chat["title"]
        return {"title": title, "markdown": to_markdown(title, chat["messages"], self.config.agent_name, chat["updated_at"])}

    async def close(self) -> None:
        await self.llm.close()
