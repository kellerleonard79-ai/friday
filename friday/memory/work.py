"""
memory/work.py
work_items: what Keller has committed to doing, from any source.

ONE TABLE, TWO ORIGINS. A Canvas-origin row is a MIRROR of canvas_assignments,
snapshotted at accept/dismiss time — this module never joins back against the
Canvas cache. See the work_items CREATE TABLE comment in memory/db.py for why
that is deliberate rather than an oversight.

BUCKETING IS NOT DONE HERE. "Now" (due within 48h, overdue pinned first),
"Upcoming" (has a date, later than that) and "Sometime" (no date) are read off
`due_at` by the client, the same "browser owns the clock" split the period
card already uses for relative due labels — a bucket boundary computed once on
the server would be wrong by the time a slow phone actually renders it.

PLAIN SQLITE, NO LEDGER. This is user-facing state, not a fact a tool
precondition checks — nothing here goes through tools/executor.py's write
machinery. Called directly by tools/work_write.py (the chat path) and by
dashboard/server.py (the dashboard path), the same split /api/commitment and
add_calendar_event already have.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

logger = logging.getLogger("friday.memory.work")


def _row(r: tuple) -> dict:
    return {
        "id": r[0], "title": r[1] or "", "source": r[2] or "",
        "source_ref": r[3], "source_url": r[4] or "",
        "due_at": r[5], "has_due_time": bool(r[6]),
        "estimated_minutes": r[7],
        "status": r[8] or "open",
        "accepted_at": r[9] or "", "completed_at": r[10] or "",
        "created_at": r[11] or "",
    }


_COLUMNS = ("id, title, source, source_ref, source_url, due_at, has_due_time, "
           "estimated_minutes, status, accepted_at, completed_at, created_at")


def get(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM work_items WHERE id = ?", (item_id,)
    ).fetchone()
    return _row(row) if row else None


def list_items(conn: sqlite3.Connection, statuses: tuple[str, ...] = ("open",),
               limit: int = 500) -> list[dict]:
    """Work items in the given statuses, undated last. Undated is kept rather
    than dropped — "no due date" is Sometime's whole reason to exist."""
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM work_items WHERE status IN ({placeholders}) "
        f"ORDER BY due_at IS NULL, due_at LIMIT ?",
        (*statuses, max(1, min(limit, 1000))),
    ).fetchall()
    return [_row(r) for r in rows]


def canvas_statuses(conn: sqlite3.Connection) -> dict[str, dict]:
    """{canvas_assignments.id: {status, work_id}} for every Canvas-origin row
    ever accepted or dismissed — what the Today panel needs to know which
    Accept/Dismiss buttons to still offer, and which items to hide outright.
    Small table; read whole rather than filtered per request."""
    try:
        rows = conn.execute(
            "SELECT source_ref, status, id FROM work_items WHERE source = 'canvas'"
        ).fetchall()
    except sqlite3.Error as e:
        logger.debug(f"canvas_statuses read failed: {e}")
        return {}
    return {ref: {"status": status, "work_id": wid} for ref, status, wid in rows}


def accept_canvas(conn: sqlite3.Connection, *, source_ref: str, title: str,
                  due_at: str | None, has_due_time: bool, source_url: str,
                  estimated_minutes: int | None = None) -> dict:
    """Mirror a Canvas assignment into Work. Idempotent: accepting an
    already-accepted or already-dismissed item is a no-op that returns the
    existing row rather than erroring — a double-tap on the Accept button
    must not raise, and re-accepting something already dismissed must not
    silently resurrect a decision Keller already made."""
    existing = conn.execute(
        "SELECT " + _COLUMNS + " FROM work_items WHERE source = 'canvas' AND source_ref = ?",
        (source_ref,),
    ).fetchone()
    if existing:
        return _row(existing)
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO work_items "
        "(title, source, source_ref, source_url, due_at, has_due_time, "
        " estimated_minutes, status, accepted_at, created_at) "
        "VALUES (?, 'canvas', ?, ?, ?, ?, ?, 'open', ?, ?)",
        (title, source_ref, source_url, due_at, 1 if has_due_time else 0,
         estimated_minutes, now, now),
    )
    conn.commit()
    return get(conn, cur.lastrowid)


def dismiss_canvas(conn: sqlite3.Connection, *, source_ref: str, title: str) -> dict:
    """Record that a Canvas item was looked at and declined. Idempotent —
    dismissing something already accepted or dismissed changes nothing and
    returns what is already there, matching accept_canvas's rule."""
    existing = conn.execute(
        "SELECT " + _COLUMNS + " FROM work_items WHERE source = 'canvas' AND source_ref = ?",
        (source_ref,),
    ).fetchone()
    if existing:
        return _row(existing)
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO work_items "
        "(title, source, source_ref, status, created_at) "
        "VALUES (?, 'canvas', ?, 'dismissed', ?)",
        (title, source_ref, now),
    )
    conn.commit()
    return get(conn, cur.lastrowid)


def add_manual(conn: sqlite3.Connection, *, title: str, due_at: str | None = None,
               has_due_time: bool = False,
               estimated_minutes: int | None = None) -> dict:
    """A task Keller typed or said, not something Canvas posted. Lands
    directly as 'open' — there is no separate accept step for your own task,
    only for something proposed on your behalf."""
    now = datetime.now().isoformat()
    cur = conn.execute(
        "INSERT INTO work_items "
        "(title, source, due_at, has_due_time, estimated_minutes, "
        " status, accepted_at, created_at) "
        "VALUES (?, 'manual', ?, ?, ?, 'open', ?, ?)",
        (title, due_at, 1 if has_due_time else 0, estimated_minutes, now, now),
    )
    conn.commit()
    return get(conn, cur.lastrowid)


def set_status(conn: sqlite3.Connection, item_id: int, status: str) -> dict | None:
    """'open' | 'done'. Completion is a plain user action here — nothing
    checks Canvas before honouring it and nothing checks Canvas afterward to
    reverse it. See the work_items table comment on why."""
    if status not in ("open", "done"):
        raise ValueError(f"unsupported status {status!r}")
    completed_at = datetime.now().isoformat() if status == "done" else None
    conn.execute(
        "UPDATE work_items SET status = ?, completed_at = ? WHERE id = ?",
        (status, completed_at, item_id),
    )
    conn.commit()
    return get(conn, item_id)


def set_estimate(conn: sqlite3.Connection, item_id: int,
                 minutes: int | None) -> dict | None:
    conn.execute(
        "UPDATE work_items SET estimated_minutes = ? WHERE id = ?",
        (minutes, item_id),
    )
    conn.commit()
    return get(conn, item_id)
