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


def set_many(conn: sqlite3.Connection, pairs: dict) -> None:
    """Write multiple keys in a single transaction."""
    now = datetime.now().isoformat()
    for key, value in pairs.items():
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, str(value), now),
        )
    conn.commit()
