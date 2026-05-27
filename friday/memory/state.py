"""
memory/state.py
Helpers for the system_state table. Replaces state.json entirely.
"""

import sqlite3
from datetime import datetime


def get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM system_state WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, str(value), datetime.now().isoformat()),
    )
    conn.commit()
