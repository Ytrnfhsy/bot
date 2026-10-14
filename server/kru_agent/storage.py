"""SQLite-сховище: довготривала пам'ять (FTS5) та чати (кожна розмова зберігається автоматично).

Усі записи мають власника (owner) — користувача, якому належать.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_WORD = re.compile(r"\w{3,}", re.UNICODE)


def _key(owner: str, sid: str) -> str:
    """Ключ чату в базі: чужий чат не відкрити, навіть знаючи його ідентифікатор."""
    return f"{owner}:{sid}"


def _user_count(messages: list[dict[str, Any]]) -> int:
    return sum(
        1 for m in messages
        if m.get("role") == "user" and not str(m.get("content") or "").startswith(("TOOL RESULT", "[view_image]"))
    )


def default_title(messages: list[dict[str, Any]]) -> str:
    for m in messages:
        if m.get("role") == "user" and m.get("content"):
            first = " ".join(str(m["content"]).split())
            return first[:60] + ("…" if len(first) > 60 else "")
    return "Нова розмова"


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._fts = True
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS memories ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',"
                " created_at REAL NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                " id TEXT PRIMARY KEY, messages TEXT NOT NULL, updated_at REAL NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS saved_chats ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, messages TEXT NOT NULL,"
                " created_at REAL NOT NULL, updated_at REAL NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS schedules ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, task TEXT NOT NULL,"
                " next_run REAL NOT NULL, repeat TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,"
                " created_at REAL NOT NULL, last_run REAL, last_status TEXT NOT NULL DEFAULT '',"
                " last_chat TEXT NOT NULL DEFAULT '')"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS user_prefs (owner TEXT PRIMARY KEY, tz TEXT NOT NULL DEFAULT '')"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS telegram ("
                " owner TEXT PRIMARY KEY, chat_id TEXT NOT NULL, linked_at REAL NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS telegram_codes ("
                " code TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL)"
            )
            had_fts = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'memories_fts'"
            ).fetchone() is not None
            try:
                self._db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
                    " content, tags, content='memories', content_rowid='id', tokenize='unicode61')"
                )
                self._db.executescript(
                    """
                    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                      INSERT INTO memories_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                      INSERT INTO memories_fts(memories_fts, rowid, content, tags)
                      VALUES ('delete', old.id, old.content, old.tags);
                    END;
                    """
                )
                if not had_fts:  # таблицю пошуку створено пізніше за записи — проіндексувати наявні
                    self._db.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            except sqlite3.OperationalError:
                self._fts = False
            self._migrate()

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Старі бази: додати власника й метадані чатів, перенести «збережені чати» в звичайні чати."""
        mcols = self._columns("memories")
        for name, ddl in (
            ("owner", "TEXT NOT NULL DEFAULT 'owner'"),
            ("embedding", "BLOB"),  # вектор для пошуку за змістом (float32)
            ("embed_model", "TEXT NOT NULL DEFAULT ''"),  # якою моделлю його пораховано
        ):
            if name not in mcols:
                self._db.execute(f"ALTER TABLE memories ADD COLUMN {name} {ddl}")
        cols = self._columns("sessions")
        for name, ddl in (
            ("owner", "TEXT NOT NULL DEFAULT 'owner'"),
            ("sid", "TEXT NOT NULL DEFAULT ''"),  # ідентифікатор, який бачить клієнт
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("auto_title", "INTEGER NOT NULL DEFAULT 1"),  # назву ще не задавали вручну
            ("count", "INTEGER NOT NULL DEFAULT 0"),  # кількість повідомлень користувача
            ("created_at", "REAL NOT NULL DEFAULT 0"),
            ("summary", "TEXT NOT NULL DEFAULT ''"),  # стислий підсумок початку довгої розмови
            ("summary_upto", "INTEGER NOT NULL DEFAULT 0"),  # скільки перших повідомлень він охоплює
            ("plan", "TEXT NOT NULL DEFAULT ''"),  # чек-лист великої задачі (JSON)
        ):
            if name not in cols:
                self._db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")
        # старі сесії: id без власника → «owner:id»
        for row in self._db.execute("SELECT id, messages, updated_at FROM sessions WHERE sid = ''").fetchall():
            messages = json.loads(row["messages"])
            self._db.execute(
                "UPDATE sessions SET id = ?, sid = ?, title = ?, count = ?, created_at = ? WHERE id = ?",
                (_key("owner", row["id"]), row["id"], default_title(messages), _user_count(messages),
                 row["updated_at"], row["id"]),
            )
        if "session" not in self._columns("telegram"):  # поточний чат, у який пишуть із Telegram
            self._db.execute("ALTER TABLE telegram ADD COLUMN session TEXT NOT NULL DEFAULT ''")
        if "tz" not in self._columns("schedules"):
            self._db.execute("ALTER TABLE schedules ADD COLUMN tz TEXT NOT NULL DEFAULT ''")
        self._db.execute("CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(owner, updated_at)")
        if "saved_chats" in {r["name"] for r in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            for row in self._db.execute("SELECT * FROM saved_chats").fetchall():
                messages = json.loads(row["messages"])
                sid = f"saved-{row['id']}"
                self._db.execute(
                    "INSERT OR IGNORE INTO sessions(id, owner, sid, messages, title, auto_title, count, created_at,"
                    " updated_at) VALUES (?, 'owner', ?, ?, ?, 0, ?, ?, ?)",
                    (_key("owner", sid), sid, row["messages"], row["title"], _user_count(messages),
                     row["created_at"], row["updated_at"]),
                )
            self._db.execute("DROP TABLE saved_chats")

    # ---------- пам'ять ----------
    def add_memory(self, content: str, tags: str = "", owner: str = "owner") -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO memories(content, tags, created_at, owner) VALUES (?, ?, ?, ?)",
                (content.strip(), tags.strip(), time.time(), owner),
            )
            return int(cur.lastrowid)

    def delete_memory(self, memory_id: int, owner: str = "owner") -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM memories WHERE id = ? AND owner = ?", (memory_id, owner))
            return cur.rowcount > 0

    def search_memories(self, query: str, limit: int = 5, owner: str = "owner") -> list[dict[str, Any]]:
        words = _WORD.findall(query.lower())
        if not words:
            return self.recent_memories(limit, owner)
        with self._lock:
            if self._fts:
                # Префіксний пошук по кожному слову, OR — щоб ловити словоформи.
                match = " OR ".join(f'"{w}"*' for w in words[:16])
                rows = self._db.execute(
                    "SELECT m.id, m.content, m.tags, m.created_at FROM memories_fts f"
                    " JOIN memories m ON m.id = f.rowid WHERE memories_fts MATCH ? AND m.owner = ?"
                    " ORDER BY bm25(memories_fts) LIMIT ?",
                    (match, owner, limit),
                ).fetchall()
            else:
                clause = " OR ".join("lower(content || ' ' || tags) LIKE ?" for _ in words[:16])
                rows = self._db.execute(
                    f"SELECT id, content, tags, created_at FROM memories WHERE owner = ? AND ({clause})"
                    " ORDER BY created_at DESC LIMIT ?",
                    (owner, *[f"%{w}%" for w in words[:16]], limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def memories_without_embedding(self, owner: str, model: str, limit: int = 32) -> list[tuple[int, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, content, tags FROM memories WHERE owner = ? AND (embedding IS NULL OR embed_model != ?)"
                " ORDER BY id LIMIT ?",
                (owner, model, limit),
            ).fetchall()
        return [(r["id"], r["content"] + (f" ({r['tags']})" if r["tags"] else "")) for r in rows]

    def set_embeddings(self, items: list[tuple[int, bytes]], model: str) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE memories SET embedding = ?, embed_model = ? WHERE id = ?",
                [(blob, model, mid) for mid, blob in items],
            )

    def memory_vectors(self, owner: str, model: str) -> list[tuple[dict[str, Any], bytes]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, content, tags, created_at, embedding FROM memories"
                " WHERE owner = ? AND embedding IS NOT NULL AND embed_model = ?",
                (owner, model),
            ).fetchall()
        return [({k: r[k] for k in ("id", "content", "tags", "created_at")}, r["embedding"]) for r in rows]

    def recent_memories(self, limit: int = 5, owner: str = "owner") -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, content, tags, created_at FROM memories WHERE owner = ? ORDER BY created_at DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- чати (зберігаються автоматично) ----------
    def load_session(self, session_id: str, owner: str = "owner") -> list[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT messages FROM sessions WHERE id = ?", (_key(owner, session_id),)).fetchone()
        return json.loads(row["messages"]) if row else []

    def save_session(self, session_id: str, messages: list[dict[str, Any]], owner: str = "owner") -> None:
        """Зберігає розмову повністю (без обрізання): після кожного кроку агента, тож збій нічого не губить."""
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions(id, owner, sid, messages, title, auto_title, count, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET messages = excluded.messages, count = excluded.count,"
                " updated_at = excluded.updated_at,"
                " title = CASE WHEN sessions.auto_title THEN excluded.title ELSE sessions.title END",
                (_key(owner, session_id), owner, session_id, json.dumps(messages, ensure_ascii=False),
                 default_title(messages), _user_count(messages), now, now),
            )

    def clear_session(self, session_id: str, owner: str = "owner") -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM sessions WHERE id = ?", (_key(owner, session_id),)).rowcount > 0

    def list_chats(self, owner: str = "owner", limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT sid, title, count, created_at, updated_at FROM sessions"
                " WHERE owner = ? AND count > 0 ORDER BY updated_at DESC LIMIT ?",
                (owner, limit),
            ).fetchall()
        return [{"id": r["sid"], "title": r["title"], "count": r["count"], "created_at": r["created_at"],
                 "updated_at": r["updated_at"]} for r in rows]

    def load_chat(self, session_id: str, owner: str = "owner") -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT sid, title, messages, created_at, updated_at FROM sessions WHERE id = ?",
                (_key(owner, session_id),),
            ).fetchone()
        if not row:
            return None
        return {"id": row["sid"], "title": row["title"], "messages": json.loads(row["messages"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def load_summary(self, session_id: str, owner: str = "owner") -> tuple[str, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT summary, summary_upto FROM sessions WHERE id = ?", (_key(owner, session_id),)
            ).fetchone()
        return (row["summary"], row["summary_upto"]) if row else ("", 0)

    def save_summary(self, session_id: str, summary: str, upto: int, owner: str = "owner") -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE sessions SET summary = ?, summary_upto = ? WHERE id = ?",
                (summary, upto, _key(owner, session_id)),
            )

    def load_plan(self, session_id: str, owner: str = "owner") -> list[dict[str, str]]:
        with self._lock:
            row = self._db.execute("SELECT plan FROM sessions WHERE id = ?", (_key(owner, session_id),)).fetchone()
        try:
            return json.loads(row["plan"]) if row and row["plan"] else []
        except ValueError:
            return []

    def save_plan(self, session_id: str, plan: list[dict[str, str]], owner: str = "owner") -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE sessions SET plan = ? WHERE id = ?",
                (json.dumps(plan, ensure_ascii=False) if plan else "", _key(owner, session_id)),
            )

    def rename_chat(self, session_id: str, title: str, owner: str = "owner") -> bool:
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE sessions SET title = ?, auto_title = 0 WHERE id = ?", (title, _key(owner, session_id))
            ).rowcount > 0

    # ---------- заплановані завдання ----------
    _SCHED_COLS = "id, owner, task, next_run, repeat, enabled, created_at, last_run, last_status, last_chat, tz"

    def add_schedule(self, owner: str, task: str, next_run: float, repeat: str = "", tz: str = "") -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO schedules(owner, task, next_run, repeat, created_at, tz) VALUES (?, ?, ?, ?, ?, ?)",
                (owner, task.strip(), next_run, repeat, time.time(), tz),
            )
            return int(cur.lastrowid)

    def list_schedules(self, owner: str, include_done: bool = False) -> list[dict[str, Any]]:
        where = "owner = ?" + ("" if include_done else " AND enabled = 1")
        with self._lock:
            rows = self._db.execute(
                f"SELECT {self._SCHED_COLS} FROM schedules WHERE {where} ORDER BY next_run", (owner,)
            ).fetchall()
        return [dict(r) for r in rows]

    def due_schedules(self, now: float) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {self._SCHED_COLS} FROM schedules WHERE enabled = 1 AND next_run <= ? ORDER BY next_run",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_schedule(self, sched_id: int, **fields: Any) -> None:
        allowed = {"next_run", "enabled", "last_run", "last_status", "last_chat"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE schedules SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                (*sets.values(), sched_id),
            )

    def cancel_schedule(self, sched_id: int, owner: str) -> bool:
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE schedules SET enabled = 0, last_status = 'скасовано' WHERE id = ? AND owner = ? AND enabled = 1",
                (sched_id, owner),
            ).rowcount > 0

    # ---------- налаштування користувача ----------
    def user_tz(self, owner: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT tz FROM user_prefs WHERE owner = ?", (owner,)).fetchone()
        return row["tz"] if row else ""

    def set_user_tz(self, owner: str, tz: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO user_prefs(owner, tz) VALUES (?, ?) ON CONFLICT(owner) DO UPDATE SET tz = excluded.tz",
                (owner, tz),
            )

    # ---------- Telegram ----------
    def set_telegram_chat(self, owner: str, chat_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO telegram(owner, chat_id, linked_at) VALUES (?, ?, ?)"
                " ON CONFLICT(owner) DO UPDATE SET chat_id = excluded.chat_id, linked_at = excluded.linked_at",
                (owner, str(chat_id), time.time()),
            )

    def telegram_owner(self, chat_id: str) -> str | None:
        """Чий це Telegram-чат (None — не прив'язаний)."""
        with self._lock:
            row = self._db.execute("SELECT owner FROM telegram WHERE chat_id = ?", (str(chat_id),)).fetchone()
        return row["owner"] if row else None

    def telegram_session(self, owner: str) -> str:
        with self._lock:
            row = self._db.execute("SELECT session FROM telegram WHERE owner = ?", (owner,)).fetchone()
        return row["session"] if row else ""

    def set_telegram_session(self, owner: str, session: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE telegram SET session = ? WHERE owner = ?", (session, owner))

    def add_telegram_code(self, owner: str, code: str, ttl: float = 600) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM telegram_codes WHERE expires < ? OR owner = ?", (time.time(), owner))
            self._db.execute("INSERT INTO telegram_codes(code, owner, expires) VALUES (?, ?, ?)",
                             (code, owner, time.time() + ttl))

    def use_telegram_code(self, code: str) -> str | None:
        """Одноразовий код прив'язки → власник (і код згорає); None — невірний чи прострочений."""
        with self._lock, self._db:
            row = self._db.execute("SELECT owner, expires FROM telegram_codes WHERE code = ?", (code,)).fetchone()
            self._db.execute("DELETE FROM telegram_codes WHERE code = ?", (code,))
        return row["owner"] if row and row["expires"] >= time.time() else None

    def telegram_chat(self, owner: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT chat_id FROM telegram WHERE owner = ?", (owner,)).fetchone()
        return row["chat_id"] if row else None
