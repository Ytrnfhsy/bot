"""Цикл агента: LLM ↔ інструменти, з підтвердженням небезпечних дій і пам'яттю."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

from .config import Config
from .images import find_image_refs, is_image, load_image
from .llm import ChatResult, OllamaClient, OpenAICompatClient, ToolsNotSupported, VisionNotSupported, create_llm
from .storage import Storage
from .tools import ToolContext, ToolError, ToolRegistry, build_registry, truncate
from .tools.memory import format_memories

log = logging.getLogger("bonsai.agent")

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
        self.ctx = ToolContext(config=config, storage=self.storage)
        self.native_tools = config.tool_mode != "json"
        self.vision = config.vision != "off"
        self.ctx.vision_ok = self.vision
        self._session_locks: dict[str, asyncio.Lock] = {}

    # ---------------- побудова контексту ----------------
    def _system_prompt(self, user_text: str) -> str:
        memories = ""
        try:
            items = self.storage.search_memories(user_text, limit=6)
        except Exception:  # noqa: BLE001
            items = []
        if items:
            memories = "\nRelevant long-term memories (may be useful):\n" + format_memories(items) + "\n"
        prompt = SYSTEM_PROMPT.format(
            now=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z (%A)"),
            workspace=self.config.workspace,
            language=self.config.language,
            name=self.config.agent_name,
            persona=f"\nPersonality: {self.config.persona}" if self.config.persona else "",
            vision=(
                "- You can SEE images: pictures attached to messages are shown to you directly. To look at an image "
                "file in the workspace (e.g. uploads/screenshot.png), call view_image. Describe what you actually see; "
                "read text from screenshots carefully.\n"
                if self.vision
                else ""
            ),
            memories=memories,
        )
        if not self.native_tools:
            prompt += JSON_PROTOCOL.format(catalog=self.registry.text_catalog())
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
    def _image_refs(self, text: str) -> list[str]:
        """Наявні в workspace картинки, згадані в тексті задачі."""
        found = []
        for ref in find_image_refs(text):
            try:
                p = self.ctx.resolve_path(ref)
            except ToolError:
                continue
            if p.is_file() and is_image(p):
                found.append(ref)
        return found

    def _attach_images(self, msgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
                    why = "переглядалось раніше" if self.vision else "поточна модель не бачить зображень"
                    notes.append(f"[зображення {path} — {why}]")
                    continue
                try:
                    loaded.append(load_image(self.ctx.resolve_path(path)))
                except Exception as e:  # noqa: BLE001
                    notes.append(f"[зображення {path} не вдалося відкрити: {e}]")
            if loaded:
                budget -= 1
                m["images"] = loaded
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
    ) -> str:
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            history = self.storage.load_session(session_id)
            user_msg: dict[str, Any] = {"role": "user", "content": user_text}
            if self.vision and (refs := self._image_refs(user_text)):
                user_msg["images"] = refs
            history.append(user_msg)
            try:
                answer = await self._loop(history, user_text, emit, confirm)
            finally:
                self.storage.save_session(session_id, self._trim_history(history))
            return answer

    async def _loop(
        self, history: list[dict[str, Any]], user_text: str, emit: EmitFunc, confirm: ConfirmFunc
    ) -> str:
        for step in range(1, self.config.max_steps + 1):
            await emit({"type": "status", "state": "thinking", "step": step})
            result = await self._call_llm(history, user_text, emit)

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

            self.ctx.pending_images.clear()
            for name, args in calls:
                output = await self._execute(name, args, emit, confirm)
                if self.native_tools:
                    history.append({"role": "tool", "tool_name": name, "content": output})
                else:
                    history.append({"role": "user", "content": f"TOOL RESULT ({name}):\n{output}"})
            # картинки від view_image — окремим повідомленням після всіх результатів інструментів
            if self.ctx.pending_images:
                paths = list(dict.fromkeys(self.ctx.pending_images))
                history.append({"role": "user", "content": "[view_image] " + ", ".join(paths), "images": paths})

        answer = (
            f"Я зробив {self.config.max_steps} кроків і не встиг завершити задачу. "
            "Уточни, будь ласка, або попроси продовжити."
        )
        history.append({"role": "assistant", "content": answer})
        await emit({"type": "final", "text": answer})
        await emit({"type": "status", "state": "idle"})
        return answer

    async def _call_llm(self, history: list[dict[str, Any]], user_text: str, emit: EmitFunc) -> ChatResult:
        async def on_token(kind: str, text: str) -> None:
            if kind == "thinking":
                await emit({"type": "thinking", "text": text})
            elif self.native_tools:
                await emit({"type": "token", "text": text})

        while True:
            trimmed = self._trim_history(history)
            msgs = trimmed if self.native_tools else self._to_json_protocol(trimmed)
            msgs = self._attach_images(msgs)
            messages = [{"role": "system", "content": self._system_prompt(user_text)}, *msgs]
            try:
                return await self.llm.chat(
                    messages, tools=self.registry.schemas() if self.native_tools else None, on_token=on_token
                )
            except VisionNotSupported:
                if not self.vision:
                    raise
                log.warning("Модель не приймає зображення — вимикаю зір (додай mmproj, щоб увімкнути)")
                self.vision = self.ctx.vision_ok = False
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

    async def _execute(self, name: str, args: dict[str, Any], emit: EmitFunc, confirm: ConfirmFunc) -> str:
        call_id = uuid.uuid4().hex[:8]
        tool = self.registry.get(name)
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
            output = await self.registry.call(self.ctx, name, args)
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

    def reset_session(self, session_id: str) -> None:
        self.storage.clear_session(session_id)

    # ---------------- збережені чати ----------------
    def save_chat(self, session_id: str, title: str | None = None, chat_id: int | None = None) -> dict[str, Any]:
        from .chats import default_title

        messages = self.storage.load_session(session_id)
        if not messages:
            raise ValueError("Розмова порожня — нічого зберігати")
        title = (title or "").strip() or default_title(messages)
        new_id = self.storage.save_chat(title, messages, chat_id)
        return {"id": new_id, "title": title}

    def list_chats(self) -> list[dict[str, Any]]:
        return self.storage.list_chats()

    def open_chat(self, chat_id: int, session_id: str) -> dict[str, Any]:
        """Відновлює збережений чат як поточну розмову (можна продовжувати)."""
        from .chats import transcript

        chat = self.storage.load_chat(chat_id)
        if chat is None:
            raise ValueError(f"Чат #{chat_id} не знайдено")
        self.storage.save_session(session_id, chat["messages"])
        return {"id": chat["id"], "title": chat["title"], "transcript": transcript(chat["messages"])}

    def delete_chat(self, chat_id: int) -> bool:
        return self.storage.delete_chat(chat_id)

    def export_chat(self, session_id: str | None = None, chat_id: int | None = None) -> dict[str, str]:
        """Markdown поточної розмови (session_id) або збереженого чату (chat_id)."""
        from .chats import default_title, to_markdown

        if chat_id is not None:
            chat = self.storage.load_chat(chat_id)
            if chat is None:
                raise ValueError(f"Чат #{chat_id} не знайдено")
            title, messages, when = chat["title"], chat["messages"], chat["updated_at"]
        else:
            messages = self.storage.load_session(session_id or "default")
            if not messages:
                raise ValueError("Розмова порожня — нічого експортувати")
            title, when = default_title(messages), None
        return {"title": title, "markdown": to_markdown(title, messages, self.config.agent_name, when)}

    async def close(self) -> None:
        await self.llm.close()
