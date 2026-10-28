"""Цикл агента: LLM ↔ інструменти, з підтвердженням небезпечних дій і пам'яттю."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Awaitable, Callable

from .config import Config
from .images import find_image_refs, is_image, load_image
from .llm import ChatResult, OllamaClient, OpenAICompatClient, ToolsNotSupported, VisionNotSupported, create_llm
from . import sandbox, ssh
from .semantic import Embedder, MemoryIndex
from .storage import Storage
from .timezones import describe_tz, get_tz, server_tz
from .tools import ToolContext, ToolError, ToolRegistry, build_registry, truncate
from .tools.memory import format_memories
from .tools.plan import PLAN_TOOLS, SUBAGENT_DENIED, format_plan
from .tools.ssh import SSH_TOOLS
from .voice import Voice

SANDBOX_TOOLS = frozenset({"run_shell", "run_python"})  # виконуються в пісочниці, якщо її увімкнено
PLAN_NUDGE_STEPS = 5  # стільки кроків без оновлення плану — і модель отримує нагадування
AUTO_FILE_TOOLS = frozenset({"write_file", "edit_file"})  # що можна дозволити без запиту (лише в робочій теці)
from .skills import prompt_section as skills_prompt
from .users import ADMIN_ONLY_TOOLS, OWNER, User, workspace_for

log = logging.getLogger("kru.agent")

EmitFunc = Callable[[dict[str, Any]], Awaitable[None]]
# (tool, summary, args) -> True/False, або "task" — дозволити цей інструмент до кінця задачі
ConfirmFunc = Callable[[str, str, dict[str, Any]], Awaitable[bool | str]]

SYSTEM_PROMPT = """Your name is {name}. You are a capable autonomous assistant agent running on the user's server.
When asked who you are, introduce yourself as {name}.{persona}
You solve tasks step by step using tools, and you finish with a clear, well-structured answer.

Current date/time (the user's local time, timezone {tz}): {now}
All times the user mentions are in this timezone — use them as is, never convert to UTC yourself.
Workspace directory (files, shell and python run here): {workspace}
Always answer in the user's language (default: {language}).

How to work:
- Think about what is needed, then act. Use tools whenever they help; do not invent facts.
- For ANY factual question about the world, recent events, prices, people, docs, etc.: use web_search,
  then open 1-3 of the best sources with fetch_url to verify, and cite source URLs in the final answer.
  If results are poor, reformulate (synonyms, English query, kind="news"). Cross-check important facts.
- For complex questions, comparisons, overviews, "research/analyse this topic" or anything needing many sources,
  use deep_research (it searches, reads several pages and returns notes with numbered sources), then write
  a structured report citing sources as [1], [2] with the URL list at the end.
- Use run_python for calculations and data processing instead of computing in your head.
- Before editing a file, read it. Prefer edit_file for small changes.
{approval}
- To read, list or search files use read_file / list_dir / search_files (no approval needed) — never run_shell
  with cat/grep/head/sed for that. For big texts read in chunks (start_line/max_lines) and write results with
  write_file (append=true for the next parts).
- Big tasks (3+ distinct steps: research + code + report, multi-file changes, translating a book chapter by
  chapter, etc.): FIRST call update_plan with a short checklist (first step in_progress, the rest pending). Then
  after finishing EACH step call mark_step with its number — the user watches this checklist live. Use
  update_plan again only to change the list itself. Skip the plan for simple questions.
- delegate: hand a self-contained subtask that needs lots of reading/searching (several files or pages) to a
  sub-agent with a fresh context; you get only its result. Describe the subtask fully — it cannot see this chat.
- Scheduling: when the user wants something done at a specific time or regularly ("at 18:00…", "every morning…",
  "remind me tomorrow…"), call schedule_task with an exact local time computed from the current date/time above and
  a complete self-contained task (it runs later without this chat). Confirm what and when. list_scheduled /
  cancel_scheduled to review or cancel. When asked which tasks are scheduled, ALWAYS call list_scheduled and report
  every item it returns — never answer from memory of this chat.
- send_telegram sends a message to the user's Telegram — use it when asked to send/notify "в телеграм"
  (path= attaches a file from the workspace).
- To get a file from the internet (image, PDF, archive…) use download_url with a direct link — not curl or
  fetch_url. Use relative paths for workspace files.
- When you create a file the user will want to keep (report, document, table, image, archive, translation…),
  call send_file with it: the user gets a one-click download button. Then answer briefly — don't paste its content.
- Long-term memory: when the user tells you something durable — about themselves, their preferences, family/people,
  plans, projects, decisions or setup (e.g. "my daughter is 5", "I prefer short answers", "the server runs Fedora") —
  save it right away with `remember` as one short self-contained fact, without asking. Do not save trivia, one-off
  requests or secrets (passwords, tokens, keys). If `remember` shows a similar memory that is now outdated or
  contradicts the new fact, `forget` the old one. Use `recall` (it searches by meaning) when past context may help.
- When the task is done, reply with the final answer only (no tool call). Be concise but complete; use Markdown.
{vision}{skills}{memories}{summary}{plan}"""

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


# що варто обміркувати (режим «авто»): код, розрахунки, аналіз, планування, «подумай»
_HEAVY = re.compile(
    r"поясни|проаналіз|аналіз|порівня|розплану|сплану|склади план|план дій|код\b|коду|кодом|скрипт|програм|"
    r"функці|алгоритм|розрахуй|розрахун|порахуй|обчисл|доведи|розв.яжи|рівнянн|оптиміз|рефактор|баг|дебаг|"
    r"дослід|стратегі|поміркуй|подумай|обдумай|крок за кроком|детально|розбери|"
    r"объясни|проанализ|сравни|посчитай|подумай|explain|analy[sz]|compare|\bcode\b|script|debug|"
    r"calculat|prove|step by step|think",
    re.IGNORECASE)
_MATH = re.compile(r"\d\s*[-+*/^×÷]\s*\(?\d")


def wants_thinking(text: str) -> bool:
    """Режим «авто»: на прості запити («котра година», «привіт», «знайди погоду») — одразу відповідь, без
    прихованих міркувань (секунди очікування); на складні — міркувати."""
    t = text.strip()
    return len(t) > 400 or "```" in t or bool(_HEAVY.search(t)) or bool(_MATH.search(t))


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


NOTE_PREFIX = "[Context note"  # службова примітка агента (час, план) — не повідомлення користувача
SERVICE_PREFIXES = ("TOOL RESULT", "[view_image]", NOTE_PREFIX)


def _is_user_turn(m: dict[str, Any]) -> bool:
    """Справжнє повідомлення користувача (не результат інструмента в JSON-протоколі й не службова примітка)."""
    return m.get("role") == "user" and not str(m.get("content") or "").startswith(SERVICE_PREFIXES)


def _msg_size(m: dict[str, Any]) -> int:
    return (
        len(m.get("content") or "")
        + len(json.dumps(m.get("tool_calls", ""), ensure_ascii=False))
        + 2500 * len(m.get("images") or [])  # картинка ≈ кілька тисяч токенів
    )


SUMMARY_PROMPT = """You maintain a running summary of a long conversation between a user and an AI assistant ({name}).
Merge the previous summary with the new messages into ONE updated summary, written in {language}.
Keep: what the user wants and why, facts about the user, decisions made, important results and numbers,
file names/paths, URLs, commands that worked, open questions and unfinished tasks. Drop greetings, chit-chat,
raw tool output and anything already superseded. Use compact bullet points, at most ~350 words.
Reply with the summary only."""

SUMMARY_CHUNK = 40000  # символів нових повідомлень за один виклик підсумовування


def _render_for_summary(msgs: list[dict[str, Any]]) -> str:
    lines = []
    for m in msgs:
        role, content = m.get("role"), strip_think(str(m.get("content") or ""))
        if role == "user" and content.startswith(NOTE_PREFIX):
            continue  # службова примітка (час, план) — у підсумку не потрібна
        if role == "user" and _is_user_turn(m):
            lines.append(f"USER: {content[:4000]}")
        elif role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                args = json.dumps(_normalize_args(fn.get("arguments")), ensure_ascii=False)
                lines.append(f"ASSISTANT called {fn.get('name')}({args[:300]})")
            if content:
                lines.append(f"ASSISTANT: {content[:4000]}")
        elif content:  # результат інструмента
            lines.append(f"TOOL RESULT ({m.get('tool_name', 'tool')}): {content[:1200]}")
    return "\n".join(lines)


@dataclass
class Turn:
    """Одна задача в чаті: історія, підсумок її початку та знайдені спогади."""

    session_id: str
    history: list[dict[str, Any]]
    user_text: str
    ctx: ToolContext
    persist: Callable[[], None] = lambda: None
    think: bool | None = None  # None — авто: міркувати лише над складними задачами (wants_thinking)
    summary: str = ""
    summary_upto: int = 0  # скільки перших повідомлень history охоплює summary
    memories: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, str]] = field(default_factory=list)  # чек-лист великої задачі (update_plan)
    subagent: bool = False  # це підзадача, доручена субагенту (delegate)
    steps_since_plan: int = 0  # кроків без оновлення плану — для нагадування моделі
    trim_from: int = 0  # з якого повідомлення history модель бачить розмову (обрізання «стрибками»)
    note_time: str = ""  # що вже сказано моделі в службових примітках цієї задачі
    note_plan: str = ""
    note_reminder: int = 0
    heavy: bool = False  # задача схожа на складну — у режимі «авто» модель міркує
    tool_error: bool = False  # на попередньому кроці інструмент повернув помилку — варто подумати
    stats: dict[str, Any] = field(default_factory=lambda: {"steps": 0, "think_steps": 0, "thinking": 0,
                                                           "answer": 0, "started": time.monotonic()})


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
        self.embedder = Embedder(config)
        self.memory = MemoryIndex(self.storage, self.embedder, config.memory_min_score)
        self.native_tools = config.tool_mode != "json"
        self.vision = config.vision != "off"
        self.vision_reason = ""  # чому зору немає (для користувача)
        self._vision_checked = 0.0
        self._session_locks: dict[str, asyncio.Lock] = {}
        self.voice = Voice(config)

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
            # у пісочниці shell і Python безпечні й для звичайних користувачів (бачать лише свою теку)
            denied_tools=frozenset() if user.is_admin else (
                SSH_TOOLS | (frozenset() if sandbox.enabled(self.config) else ADMIN_ONLY_TOOLS)),
            vision_ok=self.vision,
            memory=self.memory,
            llm=self.llm,
        )

    # ---------------- побудова контексту ----------------
    def _system_prompt(self, turn: Turn) -> str:
        """Режим cache_friendly: лише те, що не змінюється протягом задачі (дата без часу; без плану, спогадів
        і нагадувань — вони в примітці _context_note). Тоді llama-server бере вже оброблений початок
        розмови з кешу, а не перераховує весь контекст на кожному кроці."""
        ctx = turn.ctx
        stable = self.config.cache_friendly and not turn.subagent
        memories = ""
        if turn.memories and not stable:
            memories = "\nRelevant long-term memories (may be useful):\n" + format_memories(turn.memories) + "\n"
        summary = ""
        if turn.summary and turn.summary_upto:
            summary = (
                "\nSummary of the earlier part of THIS conversation (older messages were compressed to save context):\n"
                + turn.summary + "\n"
                "Exact details from that part (numbers, wording, translated terms, code, tool output) are not in the "
                "summary — when you need them, use search_chat / read_chat instead of guessing.\n"
            )
        prompt = SYSTEM_PROMPT.format(
            now=(datetime.now(ctx.tz).strftime("%Y-%m-%d (%A); the exact current time is in the latest context note "
                                               "in the conversation")
                 if stable else datetime.now(ctx.tz).strftime("%Y-%m-%d %H:%M (%A)")),
            tz=describe_tz(ctx.tz or server_tz(), ctx.tz_name),
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
            skills=skills_prompt(ctx.workspace) if not turn.subagent else "",
            summary=summary,
            plan="" if stable else self._plan_prompt(turn),
            approval=(
                "- run_shell and run_python run in an isolated Docker SANDBOX: only the workspace is visible (at the same "
                "path as above, the current directory), no access to the host system; `pip install <pkg>` works and "
                "persists. They need no approval — use them freely for calculations, data and file processing. "
                "write_file and edit_file may require user approval; if the user declines, adapt or ask."
                if sandbox.enabled(self.config) else
                "- run_shell, run_python, write_file and edit_file require user approval; if the user declines, "
                "adapt or ask."
            ) + self._ssh_prompt(ctx),
        )
        if not self.native_tools:
            prompt += JSON_PROTOCOL.format(catalog=self.registry.text_catalog(ctx.denied_tools))
        return prompt

    def _add_context_note(self, turn: Turn) -> None:
        """Режим cache_friendly: те, що змінюється (час, план, нагадування, спогади), — окремим службовим
        повідомленням, що ЛИШАЄТЬСЯ в історії. Контекст тоді лише дописується в кінець, і llama-server бере
        весь попередній промпт із кешу — для гібридних моделей (Qwen3.5/3.6: рекурентні шари) це єдиний спосіб:
        вони не вміють «відмотати» стан до середини, лише продовжити з кінця попереднього запиту.
        Нова примітка — лише коли щось змінилося, і лише змінене (план не повторюється щокроку)."""
        ctx = turn.ctx
        parts = []
        now = datetime.now(ctx.tz).strftime("%Y-%m-%d %H:%M (%A)")
        if now != turn.note_time:
            parts.append(f"Current local time: {now}")
        if not turn.note_time and turn.memories:  # перша примітка задачі
            parts.append("Relevant long-term memories (may be useful):\n" + format_memories(turn.memories))
        first = not turn.note_time
        plan = self._plan_text(turn)
        if plan and plan != turn.note_plan:
            parts.append(plan)
        elif first and not plan and any(str(m.get("content", "")).startswith(NOTE_PREFIX) and "Current plan" in
                                        str(m.get("content", "")) for m in turn.history):
            parts.append("There is no active plan for this new task (plans in earlier notes are finished).")
        elif self._needs_reminder(turn) and turn.note_reminder != turn.steps_since_plan:
            turn.note_reminder = turn.steps_since_plan
            parts.append(self._reminder(turn))
        turn.note_time, turn.note_plan = now, plan or turn.note_plan
        if parts:
            note = {"role": "user", "content": f"{NOTE_PREFIX} from the system — not written by the user]\n"
                    + "\n\n".join(parts)}
            if first and turn.history and _is_user_turn(turn.history[-1]):
                turn.history.insert(len(turn.history) - 1, note)  # спершу контекст, потім сама задача
            else:
                turn.history.append(note)

    def _plan_prompt(self, turn: Turn) -> str:
        if turn.subagent:
            return (
                "\nYou are a SUB-AGENT: the main agent delegated one subtask to you (the user message). Do only it, "
                "then reply with a concise but complete result — facts, numbers, file paths, conclusions. The main "
                "agent sees ONLY your final answer, not your tool calls.\n"
            )
        if not turn.plan:
            return ""
        text = "\n" + self._plan_text(turn) + "\n"
        if self._needs_reminder(turn):
            text += self._reminder(turn) + "\n"
        return text

    @staticmethod
    def _plan_text(turn: Turn) -> str:
        if not turn.plan:
            return ""
        return (
            "Current plan of this task (the user sees it as a checklist). Work on the in_progress step; as soon as "
            "a step is finished call mark_step with its number (the next step becomes in_progress automatically):\n"
            + format_plan(turn.plan)
        )

    @staticmethod
    def _needs_reminder(turn: Turn) -> bool:
        unfinished = any(s["status"] in {"pending", "in_progress"} for s in turn.plan)
        return unfinished and turn.steps_since_plan >= PLAN_NUDGE_STEPS

    @staticmethod
    def _reminder(turn: Turn) -> str:
        return (
            f"REMINDER: you have made {turn.steps_since_plan} steps without updating the plan. If any step above "
            "is already done, call mark_step for it NOW (you can pass several numbers at once)."
        )

    def _step_limit(self, turn: Turn) -> int:
        """Кроків на задачу: велика задача з планом отримує більше — по ~8 на кожен пункт."""
        if turn.subagent:
            return self.config.sub_max_steps
        if not turn.plan:
            return self.config.max_steps
        return max(self.config.max_steps, min(150, 8 * len(turn.plan) + 10))

    async def _delegate(self, turn: Turn, task: str, context: str, emit: EmitFunc, confirm: ConfirmFunc) -> str:
        """Субагент: чистий контекст, ті самі інструменти (без плану й делегування), результат — лише текст."""
        ctx = turn.ctx
        sub_ctx = replace(
            ctx, denied_tools=ctx.denied_tools | SUBAGENT_DENIED, pending_images=[],
            plan_hook=None, plan_get=None, delegate_hook=None, progress=None,
        )
        passthrough = {"tool_call", "tool_result", "confirm_request", "progress", "vision", "file"}

        async def sub_emit(ev: dict[str, Any]) -> None:
            kind = ev.get("type")
            if kind in passthrough:
                await emit({**ev, "sub": True})
            elif kind == "status" and ev.get("state") in {"working", "waiting_confirm"}:
                await emit(ev)
            elif kind == "status" and ev.get("state") == "thinking":
                await emit({"type": "progress", "tool": "delegate", "text": f"субагент думає (крок {ev.get('step', 1)})"})

        text = task + (f"\n\nКонтекст від основного агента:\n{context}" if context else "")
        sub = Turn("", [{"role": "user", "content": text}], text, sub_ctx, think=turn.think, subagent=True)
        await emit({"type": "progress", "tool": "delegate", "text": "субагент починає: " + task[:80]})
        answer = await self._loop(sub, sub_emit, confirm)
        return f"Результат субагента:\n{answer}"

    def _trim_history(self, history: list[dict[str, Any]], budget: int | None = None) -> list[dict[str, Any]]:
        """Жорстке обрізання з початку — запасний варіант, коли підсумок не допоміг або вимкнений."""
        budget = self.config.max_history_chars if budget is None else budget
        msgs = list(history)
        total = sum(_msg_size(m) for m in msgs)
        while msgs and total > budget and len(msgs) > 1:
            total -= _msg_size(msgs.pop(0))
        # контекст має починатися з повідомлення користувача
        while msgs and msgs[0].get("role") != "user":
            msgs.pop(0)
        return msgs

    def _budget(self, summary: str) -> int:
        """Скільки символів історії влазить поруч із підсумком (підсумок не з'їдає більше половини)."""
        return max(self.config.max_history_chars // 2, self.config.max_history_chars - len(summary))

    async def _fit_history(self, turn: Turn, emit: EmitFunc) -> list[dict[str, Any]]:
        """Що з історії піде моделі. Коли розмова не влазить у контекст, старі повідомлення
        стискаються в підсумок (з запасом, щоб не підсумовувати на кожному кроці)."""
        history = turn.history
        start = min(turn.summary_upto, len(history))
        budget = self._budget(turn.summary)
        total = sum(_msg_size(m) for m in history[start:])
        if total > budget and self.config.summarize:
            target, cut, rest = int(budget * 0.6), start, total
            while rest > target and cut < len(history) - 1:
                rest -= _msg_size(history[cut])
                cut += 1
            while cut < len(history) and not _is_user_turn(history[cut]):
                cut += 1  # різати лише перед повідомленням користувача: пари виклик/результат не рвуться
            last_user = max((i for i, m in enumerate(history) if _is_user_turn(m)), default=0)
            cut = min(cut, last_user)  # поточну задачу не підсумовуємо
            if cut > start and str(history[cut - 1].get("content") or "").startswith(NOTE_PREFIX):
                cut -= 1  # примітка (час, план) перед задачею лишається з нею
            if cut > start:
                await emit({"type": "status", "state": "summarizing"})
                try:
                    summary = await self._summarize(turn.summary, history[start:cut])
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — без підсумку просто обріжемо, як раніше
                    log.warning("Не вдалося стиснути історію: %s", e)
                    summary = ""
                if summary:
                    turn.summary, turn.summary_upto, start = summary, cut, cut
                    if turn.session_id:  # у субагента історія тимчасова
                        self.storage.save_summary(turn.session_id, summary, cut, turn.ctx.owner)
                    budget = self._budget(summary)
        if not self.config.cache_friendly:
            return self._trim_history(history[start:], budget)
        # обрізання «стрибками»: коли не влазить — відкидаємо з запасом (до 70% бюджету), щоб наступні кроки
        # мали той самий початок розмови (кеш llama-server), а не зсувалися на одне повідомлення щоразу
        begin = max(start, turn.trim_from)
        rest = sum(_msg_size(m) for m in history[begin:])
        if rest > budget:
            cut = begin
            while rest > budget * 0.7 and cut < len(history) - 1:
                rest -= _msg_size(history[cut])
                cut += 1
            # різати лише перед повідомленням користувача чи відповіддю моделі: пари виклик/результат не рвуться
            while cut < len(history) - 1 and not (_is_user_turn(history[cut]) or history[cut].get("role") == "assistant"):
                cut += 1
            turn.trim_from = begin = cut
        last_user = max((i for i, m in enumerate(history) if _is_user_turn(m)), default=-1)
        if last_user >= 0 and begin > last_user:
            # саму задачу користувача не відкидаємо ніколи — лише найстаріші кроки її виконання
            return [history[last_user], *history[begin:]]
        return self._trim_history(history[begin:], budget)  # страховка: одне величезне повідомлення

    async def _summarize(self, previous: str, msgs: list[dict[str, Any]]) -> str:
        text = _render_for_summary(msgs)
        summary = previous
        system = SUMMARY_PROMPT.format(name=self.config.agent_name, language=self.config.language)
        for i in range(0, len(text), SUMMARY_CHUNK):
            chunk = text[i : i + SUMMARY_CHUNK]
            result = await self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"PREVIOUS SUMMARY:\n{summary or '(none)'}\n\nNEW MESSAGES:\n{chunk}"},
                ],
                think=False,
            )
            summary = strip_think(result.content)
            if not summary:
                raise ValueError("модель повернула порожній підсумок")
        return summary.strip()

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
                    if m.get("detail"):
                        msg["detail"] = m["detail"]
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
            high = m.get("detail") == "high"
            m = {k: v for k, v in m.items() if k not in {"images", "detail"}}
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
                    pixels = self.config.image_max_pixels_high if high else self.config.image_max_pixels
                    loaded.append(load_image(ctx.resolve_path(path), pixels))
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
        think: bool | None = None,
        auto_files: bool = False,
        tz: str | None = None,
        retry: bool = False,
    ) -> str:
        """think — чи міркувати моделі перед відповіддю (None — як налаштовано на сервері моделі).
        retry — «Повторити»: останнє повідомлення користувача виконується заново, а попередня спроба
        (відповідь, кроки, план) прибирається з розмови; user_text тоді не потрібен."""
        ctx = self.make_context(user)
        ctx.auto_files = auto_files
        ctx.session_id = session_id
        self._apply_tz(ctx, tz)
        lock = self._session_locks.setdefault(f"{ctx.owner}:{session_id}", asyncio.Lock())
        async with lock:
            history = self.storage.load_session(session_id, ctx.owner)
            if retry:
                last = max((i for i, m in enumerate(history) if _is_user_turn(m)), default=None)
                if last is None:
                    raise ValueError("У цьому чаті ще немає задачі, яку можна повторити")
                user_text = history[last]["content"]
                if last and str(history[last - 1].get("content", "")).startswith(NOTE_PREFIX):
                    last -= 1  # і примітку перед задачею — буде нова, з поточним часом
                del history[last:]  # підсумок розмови (summary) ніколи не охоплює останню задачу
                self.storage.save_plan(session_id, [], ctx.owner)
                await emit({"type": "plan", "steps": []})
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
            summary, upto = self.storage.load_summary(session_id, ctx.owner)
            turn = Turn(session_id, history, user_text, ctx, persist, think, summary, upto)
            turn.heavy = wants_thinking(user_text)
            turn.plan = self.storage.load_plan(session_id, ctx.owner)
            if turn.plan and all(s["status"] in {"done", "skipped"} for s in turn.plan):
                turn.plan = []  # попередню велику задачу завершено — нова починається з чистого аркуша
                self.storage.save_plan(session_id, [], ctx.owner)
                await emit({"type": "plan", "steps": []})

            async def plan_hook(steps: list[dict[str, str]]) -> None:
                turn.plan = steps
                self.storage.save_plan(session_id, steps, ctx.owner)
                await emit({"type": "plan", "steps": steps})

            async def delegate_hook(task: str, context: str) -> str:
                return await self._delegate(turn, task, context, emit, confirm)

            ctx.plan_hook, ctx.delegate_hook = plan_hook, delegate_hook
            ctx.plan_get = lambda: turn.plan
            try:
                turn.memories = await self.memory.search(user_text, ctx.owner, limit=6)
            except Exception:  # noqa: BLE001 — без спогадів теж можна працювати
                log.exception("Пошук у пам'яті не вдався")
            try:
                return await self._loop(turn, emit, confirm)
            finally:
                persist()

    async def _loop(self, turn: Turn, emit: EmitFunc, confirm: ConfirmFunc) -> str:
        history, ctx = turn.history, turn.ctx
        step = 0
        while step < self._step_limit(turn):
            step += 1
            await emit({"type": "status", "state": "thinking", "step": step})
            result = await self._call_llm(turn, emit)

            calls = self._extract_calls(result)
            if not calls:
                answer = strip_think(result.content) or "(порожня відповідь моделі)"
                history.append({"role": "assistant", "content": answer})
                await emit({"type": "final", "text": answer, "stats": self._finish_stats(turn)})
                await emit({"type": "status", "state": "idle"})
                return answer

            if self.native_tools:
                history.append(result.as_message())
            else:
                history.append({"role": "assistant", "content": result.content})

            ctx.pending_images.clear()
            ctx.pending_high = False
            if any(name in PLAN_TOOLS for name, _ in calls):
                turn.steps_since_plan = 0
            elif turn.plan:
                turn.steps_since_plan += 1
            turn.tool_error = False
            for name, args in calls:
                output = await self._execute(name, args, emit, confirm, ctx)
                turn.tool_error = turn.tool_error or output.startswith("ПОМИЛКА")
                if self.native_tools:
                    history.append({"role": "tool", "tool_name": name, "content": output})
                else:
                    history.append({"role": "user", "content": f"TOOL RESULT ({name}):\n{output}"})
            # картинки від view_image — окремим повідомленням після всіх результатів інструментів
            if ctx.pending_images:
                paths = list(dict.fromkeys(ctx.pending_images))
                msg = {"role": "user", "content": "[view_image] " + ", ".join(paths), "images": paths}
                if ctx.pending_high:
                    msg["detail"] = "high"
                history.append(msg)
                await emit({"type": "vision", "state": "attached", "images": paths})
            turn.persist()

        answer = (
            f"Я зробив {step} кроків і не встиг завершити задачу. "
            "Уточни, будь ласка, або попроси продовжити."
        )
        history.append({"role": "assistant", "content": answer})
        await emit({"type": "final", "text": answer, "stats": self._finish_stats(turn)})
        await emit({"type": "status", "state": "idle"})
        return answer

    def _think_now(self, turn: Turn) -> bool:
        """Міркувати на цьому кроці? Явне налаштування користувача — як є; «авто» — лише над складною задачею,
        великою (з планом) чи коли щойно щось пішло не так."""
        if turn.think is not None:
            return turn.think
        return turn.heavy or bool(turn.plan) or turn.tool_error

    @staticmethod
    def _finish_stats(turn: Turn) -> dict[str, Any]:
        """Підсумок задачі: скільки тривала, скільки кроків і міркувань (у журнал і клієнтові — у final)."""
        st = turn.stats
        st["seconds"] = round(time.monotonic() - st.pop("started", time.monotonic()), 1)
        log.info("Задача: %.1f с, кроків %d (з міркуваннями %d), токенів міркувань ~%d, відповіді ~%d",
                 st["seconds"], st["steps"], st["think_steps"], st["thinking"], st["answer"])
        return dict(st)

    @staticmethod
    def _step_stats(turn: Turn, result: ChatResult, think: bool, seconds: float, first: float) -> None:
        """Заміри кроку — у журнал сервера й підсумок задачі (видно, де йде час)."""
        st = turn.stats
        t = (result.stats or {}).get("timings") or {}
        thinking = int(len(result.thinking) / 3.5)  # ~3,5 символу на токен (українська — більше)
        answer = int(t.get("predicted_n") or result.stats.get("eval_count") or 0) - thinking if result.stats else 0
        st["steps"] += 1
        st["think_steps"] += int(think)
        st["thinking"] += thinking
        st["answer"] += max(answer, int(len(result.content) / 3.5))
        if t.get("predicted_per_second"):
            st["gen_tps"] = round(float(t["predicted_per_second"]), 1)
        if t.get("prompt_n") is not None:
            st["prompt_tokens"] = st.get("prompt_tokens", 0) + int(t["prompt_n"])
        log.info("Крок %d: %.1f с (перший токен %.1f с), міркування %s ~%d т, читання %s т, %s т/с",
                 st["steps"], seconds, first, "так" if think else "ні", thinking,
                 t.get("prompt_n", "?"), round(float(t.get("predicted_per_second") or 0), 1) or "?")

    async def _call_llm(self, turn: Turn, emit: EmitFunc) -> ChatResult:
        ctx = turn.ctx
        think = self._think_now(turn)
        started, first = time.monotonic(), [0.0]

        async def on_token(kind: str, text: str) -> None:
            if not first[0]:
                first[0] = time.monotonic() - started
            if kind == "thinking":
                await emit({"type": "thinking", "text": text})
            elif self.native_tools:
                await emit({"type": "token", "text": text})

        if self.config.cache_friendly and not turn.subagent:
            self._add_context_note(turn)
        while True:
            trimmed = await self._fit_history(turn, emit)
            msgs = trimmed if self.native_tools else self._to_json_protocol(trimmed)
            msgs = self._attach_images(msgs, ctx)
            messages = [{"role": "system", "content": self._system_prompt(turn)}, *msgs]
            try:
                result = await self.llm.chat(
                    messages,
                    tools=self.registry.schemas(ctx.denied_tools) if self.native_tools else None,
                    on_token=on_token,
                    think=think,
                )
                self._step_stats(turn, result, think, time.monotonic() - started, first[0])
                return result
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

        if (tool and tool.dangerous and not self.config.auto_approve and not self._auto_approved(name, args, ctx)
                and not self._sandboxed(name) and not self._trusted_host(name, args)):
            await emit({"type": "status", "state": "waiting_confirm"})
            approved = await confirm(name, summary, args)
            if approved == "task" and name in AUTO_FILE_TOOLS:  # «дозволяти до кінця задачі»
                ctx.approved_tools.add(name)
            if not approved:
                msg = "Користувач ВІДХИЛИВ цю дію. Не повторюй її; запропонуй альтернативу або спитай, як діяти."
                await emit({"type": "tool_result", "id": call_id, "name": name, "ok": False, "preview": "відхилено"})
                return msg

        await emit({"type": "status", "state": "working", "tool": name})

        async def progress(text: str) -> None:
            await emit({"type": "progress", "tool": name, "text": text})

        ctx.progress = progress
        ctx.event = emit
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

    def _apply_tz(self, ctx: ToolContext, tz: str | None) -> None:
        """Пояс користувача: від клієнта (і запам'ятати) → збережений → KRU_TIMEZONE → пояс сервера."""
        if get_tz(tz):
            if self.storage.user_tz(ctx.owner) != tz:
                self.storage.set_user_tz(ctx.owner, tz)
            name = tz
        else:
            name = self.storage.user_tz(ctx.owner) or self.config.timezone
        ctx.tz = get_tz(name)
        ctx.tz_name = name if ctx.tz else ""

    def _sandboxed(self, name: str) -> bool:
        """Команда піде в ізольовану пісочницю — дозвіл не потрібен (якщо не KRU_SANDBOX_CONFIRM)."""
        return name in SANDBOX_TOOLS and sandbox.enabled(self.config) and not self.config.sandbox_confirm

    def _ssh_prompt(self, ctx: ToolContext) -> str:
        """Підключені SSH-машини — лише тим, кому SSH дозволено."""
        section = ssh.prompt_section(self.config) if SSH_TOOLS - ctx.denied_tools else ""
        return "\n" + section.rstrip("\n") if section else ""

    def _trusted_host(self, name: str, args: dict[str, Any]) -> bool:
        """SSH на машину, яку власник позначив «без запитів» (тестове середовище)."""
        if name not in SSH_TOOLS:
            return False
        host = ssh.load_hosts(self.config).get(str(args.get("host", "")).strip())
        return host is not None and not host.confirm

    @staticmethod
    def _auto_approved(name: str, args: dict[str, Any], ctx: ToolContext) -> bool:
        """Запис файлів без запиту — лише якщо користувач дозволив і лише в його робочій теці
        (старі версії зберігаються в .kru-backup). Shell і Python завжди питають."""
        if name not in AUTO_FILE_TOOLS or not (ctx.auto_files or name in ctx.approved_tools):
            return False
        try:
            path = ctx.resolve_path(str(args.get("path", "")))
        except ToolError:
            return False
        ws = ctx.workspace.resolve()
        return path != ws and ws in path.parents

    def reset_session(self, session_id: str, owner: str = OWNER) -> None:
        self.storage.clear_session(session_id, owner)

    # ---------------- чати (зберігаються автоматично) ----------------
    def list_chats(self, owner: str = OWNER) -> list[dict[str, Any]]:
        return self.storage.list_chats(owner)

    def search_chats(self, query: str, owner: str = OWNER) -> list[dict[str, Any]]:
        """Чати, де в назві чи тексті розмови є всі слова запиту (з уривком зі збігом)."""
        from .chats import search

        return search(self.storage.all_chats(owner), query)

    def open_chat(self, session_id: str, owner: str = OWNER) -> dict[str, Any]:
        """Чат для показу; клієнт далі просто пише в нього (session_id = id чату)."""
        from .chats import transcript

        chat = self.storage.load_chat(session_id, owner)
        if chat is None:
            raise ValueError("Чат не знайдено — можливо, його видалили")
        return {"id": chat["id"], "title": chat["title"], "transcript": transcript(chat["messages"]),
                "plan": self.storage.load_plan(session_id, owner)}

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
        await self.embedder.close()
