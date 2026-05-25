"""
agent/memory.py
Friday's memory system — SQLite-backed short and long-term storage.
"""

import sqlite3
import json
import os
from datetime import datetime


class Memory:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS long_term (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS short_term (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    UNIQUE(source, message_id)
                );
            """)

    # ── Long-term memory ──────────────────────────────────────────────────────

    def remember(self, key: str, value):
        """Store a fact in long-term memory."""
        now = datetime.now().isoformat()
        val = json.dumps(value)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO long_term (key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """, (key, val, now, now))

    def recall(self, key: str):
        """Retrieve a fact from long-term memory. Returns None if not found."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM long_term WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def recall_all(self) -> dict:
        """Return all long-term memory as a dict."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT key, value FROM long_term").fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    # ── Short-term memory (conversation context) ──────────────────────────────

    def add_turn(self, role: str, content: str, source: str = None):
        """Add a message turn to short-term memory."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO short_term (role, content, source, created_at)
                VALUES (?, ?, ?, ?)
            """, (role, content, source, datetime.now().isoformat()))

    def get_recent_turns(self, n: int = 20) -> list:
        """Get the last n turns for inclusion in Claude API context."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT role, content FROM short_term
                ORDER BY id DESC LIMIT ?
            """, (n,)).fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    # ── Message deduplication ─────────────────────────────────────────────────

    def is_processed(self, source: str, message_id: str) -> bool:
        """Check if a message has already been processed."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT id FROM processed_messages
                WHERE source = ? AND message_id = ?
            """, (source, message_id)).fetchone()
        return row is not None

    def mark_processed(self, source: str, message_id: str):
        """Mark a message as processed so it won't be processed again."""
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("""
                    INSERT INTO processed_messages (source, message_id, processed_at)
                    VALUES (?, ?, ?)
                """, (source, message_id, datetime.now().isoformat()))
            except sqlite3.IntegrityError:
                pass  # Already marked, that's fine
