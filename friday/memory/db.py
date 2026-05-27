"""
memory/db.py
SQLite connection and schema for Friday.
"""

import logging
import os
import sqlite3

logger = logging.getLogger("friday.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS system_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    source     TEXT,
    title      TEXT,
    body       TEXT,
    due_at     TEXT,
    urgency    TEXT,
    notified   INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS last_seen (
    source     TEXT PRIMARY KEY,
    cursor     TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id          TEXT PRIMARY KEY,
    action_type TEXT,
    payload     TEXT,
    status      TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS conversation_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    role       TEXT,
    content    TEXT,
    created_at TEXT
);
"""


class Database:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info(f"Database ready: {db_path}")

    def connection(self) -> sqlite3.Connection:
        return self._conn
