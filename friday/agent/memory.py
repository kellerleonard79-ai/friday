"""
agent/memory.py
SQLite-backed memory for Friday.

long_term  — persistent key/value facts
short_term — rolling conversation turn buffer
"""

import json
import logging
import sqlite3
from datetime import datetime

logger = logging.getLogger("friday.memory")


class Memory:
    def __init__(self, db_path: str = "memory/friday_memory.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._create_tables()
        logger.info(f"Memory initialised: {db_path}")

    def _create_tables(self):
        cur = self._conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS long_term (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS short_term (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                role       TEXT,
                content    TEXT,
                created_at TEXT
            )
        """)
        self._conn.commit()

    # ── Long-term ─────────────────────────────────────────────────────────────

    def remember(self, key: str, value) -> None:
        serialised = json.dumps(value) if not isinstance(value, str) else value
        self._conn.execute(
            "INSERT OR REPLACE INTO long_term (key, value, updated_at) VALUES (?, ?, ?)",
            (key, serialised, datetime.now().isoformat()),
        )
        self._conn.commit()

    def recall(self, key: str):
        row = self._conn.execute(
            "SELECT value FROM long_term WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return row[0]

    def recall_all(self) -> dict:
        rows = self._conn.execute("SELECT key, value FROM long_term").fetchall()
        result = {}
        for key, val in rows:
            try:
                result[key] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                result[key] = val
        return result

    def forget(self, key: str) -> None:
        self._conn.execute("DELETE FROM long_term WHERE key = ?", (key,))
        self._conn.commit()

    # ── Short-term ────────────────────────────────────────────────────────────

    def add_turn(self, role: str, content: str) -> None:
        """role: 'user' | 'assistant'"""
        self._conn.execute(
            "INSERT INTO short_term (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, datetime.now().isoformat()),
        )
        self._conn.commit()

    def get_recent_turns(self, n: int = 20) -> list:
        rows = self._conn.execute(
            "SELECT role, content FROM short_term ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]
