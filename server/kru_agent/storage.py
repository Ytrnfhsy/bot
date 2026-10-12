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
        if "owner" not in self._columns("memories"):
            self._db.execute("ALTER TABLE memories ADD COLUMN owner TEXT NOT NULL DEFAULT 'owner'")
        cols = self._columns("sessions")
        for name, ddl in (
            ("owner", "TEXT NOT NULL DEFAULT 'owner'"),
            ("sid", "TEXT NOT NULL DEFAULT ''"),  # ідентифікатор, який бачить клієнт
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("auto_title", "INTEGER NOT NULL DEFAULT 1"),  # назву ще не задавали вручну
            ("count", "INTEGER NOT NULL DEFAULT 0"),  # кількість повідомлень користувача
            ("created_at", "REAL NOT NULL DEFAULT 0"),
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

    def rename_chat(self, session_id: str, title: str, owner: str = "owner") -> bool:
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE sessions SET title = ?, auto_title = 0 WHERE id = ?", (title, _key(owner, session_id))
            ).rowcount > 0
