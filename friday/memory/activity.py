"""
memory/activity.py
Best-effort recorders for the activity-capture tables (see memory/db.py).

These power the dashboard "Today" surface: llm_exchanges, tool_calls,
briefings_sent, urgent_alerts_sent. Every function here is instrumentation —
it must NEVER raise into the caller's hot path. On any failure we log at debug
and move on; a dropped activity row is always preferable to a broken briefing,
tool call, or chat turn.

All timestamps are local-naive ISO strings (datetime.now().isoformat()), matching
the convention already used by conversation_history / events / pending_actions.
"""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger("friday.activity")

# Preview lengths — keep the feed scannable; full_* columns hold the verbatim text.
_PREVIEW = 200
_RESULT_PREVIEW = 300

# Retention for the nightly cleanup job.
_RETENTION_DAYS = 30


def _preview(text: str | None, n: int = _PREVIEW) -> str:
    s = (text or "").strip().replace("\n", " ")
    return s[:n]


def record_llm_exchange(conn: sqlite3.Connection, *, model: str, prompt: str,
                        response: str, tokens_in: int, tokens_out: int,
                        duration_ms: int, triggered_by: str,
                        profile: str | None = None, finish: str | None = None,
                        error_kind: str | None = None) -> None:
    """One row per llm.dispatch() call. Full prompt/response stored verbatim so
    /api/llm/last can show the most recent exchange exactly as it ran.

    profile/finish/error_kind come from the dispatcher and are None only for
    rows written before it existed."""
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO llm_exchanges "
            "(timestamp, model, prompt_preview, response_preview, tokens_in, "
            " tokens_out, duration_ms, triggered_by, full_prompt, full_response, "
            " profile, finish, error_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), model, _preview(prompt), _preview(response),
             int(tokens_in or 0), int(tokens_out or 0), int(duration_ms or 0),
             triggered_by, prompt or "", response or "",
             profile, finish, error_kind),
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"record_llm_exchange failed: {e}")


def normalize_message(text: str) -> str:
    """Lowercased, trimmed, whitespace collapsed — the form the duplicate-rate
    query groups on, so "Brief me" and "brief  me " count as one message."""
    return " ".join((text or "").lower().split())


def record_dispatch(conn: sqlite3.Connection, *, raw_message: str, decision) -> int | None:
    """One row per dispatcher decision. Returns the row id so the caller can
    flag it if the miss-recovery fallback later fires; None if unrecorded.

    `decision` is an agent.dispatcher.DispatchResult. tokens_in/out pass
    through as None on failure rather than being coerced to 0 — see the table
    comment in memory/db.py.
    """
    if conn is None:
        return None
    try:
        norm = normalize_message(raw_message)
        cur = conn.execute(
            "INSERT INTO dispatch_log "
            "(created_at, raw_message, normalized_message, message_hash, "
            " selected_tools, tool_count, provider, model, latency_ms, "
            " tokens_in, tokens_out, outcome, fallback_triggered) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (datetime.now().isoformat(), raw_message or "", norm,
             hashlib.sha256(norm.encode()).hexdigest(),
             json.dumps(decision.tools), len(decision.tools),
             decision.provider, decision.model, int(decision.latency_ms or 0),
             decision.tokens_in, decision.tokens_out, decision.outcome),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        logger.debug(f"record_dispatch failed: {e}")
        return None


def mark_dispatch_fallback(conn: sqlite3.Connection, row_id: int | None) -> None:
    """Flag a dispatch row as having needed the all-tools retry."""
    if conn is None or row_id is None:
        return
    try:
        conn.execute(
            "UPDATE dispatch_log SET fallback_triggered = 1 WHERE id = ?",
            (row_id,),
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"mark_dispatch_fallback failed: {e}")


def record_tool_call(conn: sqlite3.Connection, *, tool_name: str, args_json: str,
                     result_preview: str, duration_ms: int, triggered_by: str,
                     hop: int = 0, outcome: str = "") -> None:
    """One row per tool invocation, written by agent/turn.py."""
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO tool_calls "
            "(timestamp, tool_name, args_json, result_preview, duration_ms, "
            " triggered_by, hop, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), tool_name, args_json,
             _preview(result_preview, _RESULT_PREVIEW), int(duration_ms or 0),
             triggered_by, int(hop or 0), outcome),
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"record_tool_call failed: {e}")


def record_briefing(conn: sqlite3.Connection, *, slot: str, body: str,
                    on_time_vs_catchup: str, age_minutes: int) -> None:
    """One row per briefing actually sent."""
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO briefings_sent "
            "(timestamp, slot, body_preview, body_full, on_time_vs_catchup, age_minutes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now().isoformat(), slot, _preview(body), body or "",
             on_time_vs_catchup, int(age_minutes or 0)),
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"record_briefing failed: {e}")


def record_urgent_alert(conn: sqlite3.Connection, *, source: str, source_ref: str,
                        body: str) -> None:
    """One row per urgent interrupt fired."""
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT INTO urgent_alerts_sent "
            "(timestamp, source, source_ref, body_preview) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(), source, source_ref, _preview(body, _RESULT_PREVIEW)),
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"record_urgent_alert failed: {e}")


def cleanup_old_activity(conn: sqlite3.Connection, days: int = _RETENTION_DAYS) -> int:
    """Delete activity rows older than `days`. Returns total rows removed.
    Called nightly from friday.py's job_queue. Best-effort."""
    if conn is None:
        return 0
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    removed = 0
    for table in ("llm_exchanges", "tool_calls", "briefings_sent", "urgent_alerts_sent"):
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
            removed += cur.rowcount or 0
        except Exception as e:
            logger.debug(f"cleanup_old_activity({table}) failed: {e}")
    try:
        conn.commit()
    except Exception:
        pass
    if removed:
        logger.info(f"Activity cleanup: removed {removed} row(s) older than {days}d")
    return removed
