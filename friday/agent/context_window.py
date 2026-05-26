"""
agent/context_window.py
Sliding window context gathering.

Fetches the last N messages from a conversation thread using local sources
only — no API calls, zero tokens spent — and formats them as a compact
"Sender: Message" block for injection into FridayAgent._think() prompts.

All SQLite access is strictly parameterized; no f-strings or concatenation
touch query text.
"""

import sqlite3
import logging

logger = logging.getLogger("friday.context_window")


def gather_imessage_context(chat_identifier: str, db_path: str, limit: int = 8) -> str:
    """Return the last `limit` messages from a chat.db thread as a text block.

    Uses read-only URI connection and bound parameters throughout.
    Returns "" on any error so callers never need to guard exceptions.
    """
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                """
                SELECT m.text, m.is_from_me
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.rowid
                JOIN chat c ON c.rowid = cmj.chat_id
                WHERE c.chat_identifier = ?
                  AND m.text IS NOT NULL
                  AND length(trim(m.text)) > 0
                ORDER BY m.date DESC
                LIMIT ?
                """,
                (chat_identifier, limit),
            ).fetchall()
    except Exception as e:
        logger.debug(f"iMessage context window query failed: {e}")
        return ""

    if not rows:
        return ""

    lines = []
    for text, is_from_me in reversed(rows):  # oldest-first reading order
        label = "Friday" if is_from_me else "You"
        lines.append(f"{label}: {text.strip()}")
    return "\n".join(lines)


def gather_groupme_context(group_id: str, groupme_channel, limit: int = 8) -> str:
    """Return the last `limit` messages from a GroupMe group's local buffer.

    Reads from groupme_channel._context_buffer[group_id] (a deque populated
    during poll cycles). Returns "" if the buffer is absent or empty.
    """
    buf = getattr(groupme_channel, "_context_buffer", {})
    msgs = list(buf.get(group_id, []))[-limit:]
    if not msgs:
        return ""
    return "\n".join(f"{m['sender_name']}: {m['text']}" for m in msgs)
