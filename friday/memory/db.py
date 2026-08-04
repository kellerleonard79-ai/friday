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
    id              TEXT PRIMARY KEY,
    source          TEXT,
    title           TEXT,
    body            TEXT,
    due_at          TEXT,
    urgency         TEXT,
    processed       INTEGER DEFAULT 0,
    notified        INTEGER DEFAULT 0,
    calendar_synced INTEGER DEFAULT 0,
    event_extracted INTEGER DEFAULT 0,
    created_at      TEXT
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

CREATE TABLE IF NOT EXISTS synced_events (
    google_event_id  TEXT PRIMARY KEY,
    calendar_name    TEXT,
    apple_event_id   TEXT,
    synced_at        TEXT
);

-- ── Activity capture (powers the dashboard "Today" surface) ──────────────────
-- These four tables record what Friday actually DID, as opposed to the
-- operational flags (events.processed/notified, system_state counters) that
-- only record that something happened. A nightly cleanup job trims each to the
-- last 30 days. All writes are best-effort (memory/activity.py) — instrumentation
-- never raises into the hot path.

-- Every LLM call (chat, briefing, urgency tagging, quip) writes one row here.
-- Per-day stats and /api/llm/last are computed from these rows — no rollup table.
CREATE TABLE IF NOT EXISTS llm_exchanges (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT,
    model            TEXT,
    prompt_preview   TEXT,
    response_preview TEXT,
    tokens_in        INTEGER,
    tokens_out       INTEGER,
    duration_ms      INTEGER,
    triggered_by     TEXT,    -- user_message | briefing_* | poll | ...
    full_prompt      TEXT,
    full_response    TEXT
);

-- Every Gemini function-call invocation (wrapped in agent/tools.py).
CREATE TABLE IF NOT EXISTS tool_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT,
    tool_name      TEXT,
    args_json      TEXT,
    result_preview TEXT,
    duration_ms    INTEGER,
    triggered_by   TEXT
);

-- One row per tool-dispatcher decision (agent/dispatcher.py), including the
-- failures — those are the interesting rows. Exists to answer two questions:
-- how often the same message is dispatched twice (is a cache worth building),
-- and how often the dispatcher missed (fallback_triggered).
--
-- tokens_in/tokens_out are NULL, never 0, when the call failed: a failed call
-- has no usage metadata, and writing 0 would average in as a free call.
CREATE TABLE IF NOT EXISTS dispatch_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT,
    raw_message         TEXT,     -- verbatim, as received
    normalized_message  TEXT,     -- lowercased, trimmed, whitespace collapsed
    message_hash        TEXT,     -- sha256 of normalized_message
    selected_tools      TEXT,     -- JSON array, as returned
    tool_count          INTEGER,
    provider            TEXT,
    model               TEXT,
    latency_ms          INTEGER,  -- dispatcher call only
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    outcome             TEXT,     -- ok | parse_fail | timeout | dropped_invalid
                                  -- | provider_error | disabled
    fallback_triggered  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_dispatch_hash ON dispatch_log (message_hash);

-- Every briefing actually sent (morning/evening), with the full body.
CREATE TABLE IF NOT EXISTS briefings_sent (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp          TEXT,
    slot               TEXT,    -- 'morning' | 'evening' | 'on_demand'
    body_preview       TEXT,
    body_full          TEXT,
    on_time_vs_catchup TEXT,    -- 'on_time' | 'catchup' | 'override'
    age_minutes        INTEGER
);

-- Every urgent interrupt actually fired.
CREATE TABLE IF NOT EXISTS urgent_alerts_sent (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT,
    source       TEXT,    -- 'groupme' | 'canvas' | ...
    source_ref   TEXT,    -- events.id that triggered it
    body_preview TEXT
);
"""


class Database:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        logger.info(f"Database ready: {db_path}")

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(events)")}
        if "processed" not in cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN processed INTEGER DEFAULT 0")
            logger.info("Migration: added events.processed column")
        if "calendar_synced" not in cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN calendar_synced INTEGER DEFAULT 0")
            # Backfill pre-existing canvas rows to synced=1 so the new auto-sync
            # doesn't retroactively dump historical assignments into Apple Calendar.
            self._conn.execute(
                "UPDATE events SET calendar_synced = 1 WHERE source = 'canvas'"
            )
            logger.info("Migration: added events.calendar_synced column (canvas rows backfilled)")
        if "event_extracted" not in cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN event_extracted INTEGER DEFAULT 0")
            # Backfill all existing rows to extracted=1 — we don't want the new
            # pass to retroactively scan every historical groupme message.
            self._conn.execute("UPDATE events SET event_extracted = 1")
            logger.info("Migration: added events.event_extracted column (existing rows backfilled)")

        # pending_actions.resolved_at: when a row left 'pending' (confirmed /
        # cancelled / failed). NULL for existing rows — we can't reconstruct a
        # historical resolution time, so the activity feed treats them as undated.
        pa_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(pending_actions)")}
        if "resolved_at" not in pa_cols:
            self._conn.execute("ALTER TABLE pending_actions ADD COLUMN resolved_at TEXT")
            logger.info("Migration: added pending_actions.resolved_at column")

    def connection(self) -> sqlite3.Connection:
        return self._conn
