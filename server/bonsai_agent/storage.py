"""SQLite-сховище: довготривала пам'ять (FTS5) та історія сесій."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_WORD = re.compile(r"\w{3,}", re.UNICODE)


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
            except sqlite3.OperationalError:
                self._fts = False

    # ---------- пам'ять ----------
    def add_memory(self, content: str, tags: str = "") -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO memories(content, tags, created_at) VALUES (?, ?, ?)",
                (content.strip(), tags.strip(), time.time()),
            )
            return int(cur.lastrowid)

    def delete_memory(self, memory_id: int) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cur.rowcount > 0

    def search_memories(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        words = _WORD.findall(query.lower())
        if not words:
            return self.recent_memories(limit)
        with self._lock:
            if self._fts:
                # Префіксний пошук по кожному слову, OR — щоб ловити словоформи.
                match = " OR ".join(f'"{w}"*' for w in words[:16])
                rows = self._db.execute(
                    "SELECT m.id, m.content, m.tags, m.created_at FROM memories_fts f"
                    " JOIN memories m ON m.id = f.rowid WHERE memories_fts MATCH ?"
                    " ORDER BY bm25(memories_fts) LIMIT ?",
                    (match, limit),
                ).fetchall()
            else:
                clause = " OR ".join("lower(content || ' ' || tags) LIKE ?" for _ in words[:16])
                rows = self._db.execute(
                    f"SELECT id, content, tags, created_at FROM memories WHERE {clause}"
                    " ORDER BY created_at DESC LIMIT ?",
                    (*[f"%{w}%" for w in words[:16]], limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def recent_memories(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, content, tags, created_at FROM memories ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- сесії ----------
    def load_session(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT messages FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return json.loads(row["messages"]) if row else []

    def save_session(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions(id, messages, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET messages = excluded.messages, updated_at = excluded.updated_at",
                (session_id, json.dumps(messages, ensure_ascii=False), time.time()),
            )

    def clear_session(self, session_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    # ---------- збережені чати ----------
    def save_chat(self, title: str, messages: list[dict[str, Any]], chat_id: int | None = None) -> int:
        now = time.time()
        data = json.dumps(messages, ensure_ascii=False)
        with self._lock, self._db:
            if chat_id is not None:
                cur = self._db.execute(
                    "UPDATE saved_chats SET title = ?, messages = ?, updated_at = ? WHERE id = ?",
                    (title, data, now, chat_id),
                )
                if cur.rowcount:
                    return chat_id
            cur = self._db.execute(
                "INSERT INTO saved_chats(title, messages, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (title, data, now, now),
            )
            return int(cur.lastrowid)

    def list_chats(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, title, messages, created_at, updated_at FROM saved_chats ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "count": sum(1 for m in json.loads(r["messages"]) if m.get("role") == "user"),
            }
            for r in rows
        ]

    def load_chat(self, chat_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, title, messages, created_at, updated_at FROM saved_chats WHERE id = ?", (chat_id,)
            ).fetchone()
        if not row:
            return None
        return {**dict(row), "messages": json.loads(row["messages"])}

    def delete_chat(self, chat_id: int) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM saved_chats WHERE id = ?", (chat_id,)).rowcount > 0
