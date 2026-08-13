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
    created_at TEXT,
    channel    TEXT
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
    full_response    TEXT,
    profile          TEXT,    -- llm/profiles.py name: CHAT | ...
    finish           TEXT,    -- stop | tool_calls | length | error
    error_kind       TEXT,    -- none | rate_limit | transient | network | fatal
    plan             TEXT     -- router/plans.py name, or NULL: no plan (CHAT as before)
);

-- One row per tool invocation, written by agent/turn.py's loop.
--
-- hop and outcome are what make this table answer the questions worth asking:
-- how many hops a turn really took (the model can request several), and how
-- often a call failed rather than merely how long it took. A table of
-- durations alone cannot distinguish a fast success from a fast rejection.
CREATE TABLE IF NOT EXISTS tool_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT,
    tool_name      TEXT,
    args_json      TEXT,
    result_preview TEXT,
    duration_ms    INTEGER,
    triggered_by   TEXT,
    hop            INTEGER,  -- 1-based; which pass through the loop
    outcome        TEXT      -- ok | <ToolErrorKind> | unknown_tool | timeout
);

-- ORPHANED. Written by agent/dispatcher.py, which the Phase III teardown
-- deleted; its last reader (tools_verify_dispatch.py) and its writers in
-- memory/activity.py are gone too. Nothing reads or writes this table.
--
-- The CREATE is kept rather than dropped because the rows are real history and
-- a migration that drops a table cannot be reverted by checking out the old
-- code. Delete it deliberately, if ever, not as a side effect.
--
-- Note this was the TOOL-SELECTION dispatcher, which chose which tools to
-- offer per message — not llm/dispatch.py. The rebuilt loop offers a profile's
-- whole scope and lets the model choose, so there is no decision to log here.
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

        # llm_exchanges.profile / finish / error_kind: the dispatcher (llm/dispatch.py)
        # records WHICH calling convention ran and HOW it ended, not just what it cost.
        # Additive on purpose — the pre-dispatcher rows are real cost history and both
        # dashboard readers name their columns explicitly, so they are unaffected.
        # Existing rows keep NULL: we cannot reconstruct a profile that did not exist.
        # llm_exchanges.plan: which router plan shape a dispatch ran under
        # (router/plans.py). NULL means no plan — either the row predates the
        # router, or the classifier fell back, and those are the same thing on
        # purpose: both ran CHAT with its own scope. A label for reading the
        # table afterward, never read back to make a decision.
        le_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(llm_exchanges)")}
        for col in ("profile", "finish", "error_kind", "plan"):
            if col not in le_cols:
                self._conn.execute(f"ALTER TABLE llm_exchanges ADD COLUMN {col} TEXT")
                logger.info(f"Migration: added llm_exchanges.{col} column")

        # tool_calls.hop / .outcome: the turn loop records which pass through
        # the loop a call was made on and how it ended, not just what it cost.
        # Existing rows keep NULL — they predate the loop, and there is no hop
        # number to reconstruct for a call the old dispatcher made.
        tc_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tool_calls)")}
        if "hop" not in tc_cols:
            self._conn.execute("ALTER TABLE tool_calls ADD COLUMN hop INTEGER")
            logger.info("Migration: added tool_calls.hop column")
        if "outcome" not in tc_cols:
            self._conn.execute("ALTER TABLE tool_calls ADD COLUMN outcome TEXT")
            logger.info("Migration: added tool_calls.outcome column")

        # tool_calls.ledger_json: what the turn had ESTABLISHED at the moment a
        # write ran — the reads it had made, and any earlier writes. NULL for
        # reads, because a read's own coverage is already its result.
        #
        # "What had Friday actually read when it wrote?" is the first question
        # anyone asks after a bad write, and until now the answer was nowhere:
        # the ledger is per-turn and its entries are cleared in run_turn's
        # finally block, so it was gone before anyone could ask.
        if "ledger_json" not in tc_cols:
            self._conn.execute("ALTER TABLE tool_calls ADD COLUMN ledger_json TEXT")
            logger.info("Migration: added tool_calls.ledger_json column")

        # conversation_history.channel: which surface a line came from —
        # "telegram", "dashboard". Recorded, never used to partition the
        # window a turn reads: a message typed in the dashboard has to be in
        # scope when the user asks about it over Telegram an hour later. They
        # are one conversation with one person, and a window split by surface
        # would make Friday forget things for a reason the user cannot see.
        # Empty for every row written before this column existed, which is
        # honest — those predate there being a second channel.
        ch_cols = {r[1] for r in
                   self._conn.execute("PRAGMA table_info(conversation_history)")}
        if "channel" not in ch_cols:
            self._conn.execute(
                "ALTER TABLE conversation_history ADD COLUMN channel TEXT")
            logger.info("Migration: added conversation_history.channel column")

        # pending_actions.resolved_at: when a row left 'pending' (confirmed /
        # cancelled / failed). NULL for existing rows — we can't reconstruct a
        # historical resolution time, so the activity feed treats them as undated.
        pa_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(pending_actions)")}
        if "resolved_at" not in pa_cols:
            self._conn.execute("ALTER TABLE pending_actions ADD COLUMN resolved_at TEXT")
            logger.info("Migration: added pending_actions.resolved_at column")

        # A permission card is now a proposal to run a specific tool with
        # specific arguments, so the row has to carry them. Before this, a
        # pending row was a calendar event dict in `payload` and the only thing
        # that could act on it was hardcoded.
        #
        # `arguments_json` is what runs on confirm. THE STORED ARGUMENTS, never
        # re-extracted ones: the user approved a set of values, and asking the
        # model again at confirm time means they could approve one event and
        # get another.
        #
        # `proposal` is the card text exactly as the user saw it. Kept so the
        # dashboard and the activity feed show what was approved rather than
        # rebuilding it and risking drift.
        #
        # `expires_at` is the TTL. A card with no expiry is a button that
        # silently still works days later, long after the user has forgotten
        # what it was for.
        for col, decl in (("tool_name", "TEXT"), ("arguments_json", "TEXT"),
                          ("proposal", "TEXT"), ("expires_at", "TEXT"),
                          ("turn_id", "TEXT")):
            if col not in pa_cols:
                self._conn.execute(
                    f"ALTER TABLE pending_actions ADD COLUMN {col} {decl}")
                logger.info(f"Migration: added pending_actions.{col} column")

        # recent_writes: the idempotency ledger, keyed on a content fingerprint
        # rather than on anyone's identifier. It answers "did this exact write
        # just happen, or might it have" WITHOUT asking the service — which
        # matters because the case it exists for is a write whose service call
        # timed out, and a retry cannot rely on reaching the thing that just
        # failed to answer.
        #
        # `identifier` is NULL for an unknown outcome. That is the whole point
        # of the row: we know a write was attempted and we do not know its id.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS recent_writes (
                fingerprint TEXT PRIMARY KEY,
                created_at  TEXT,
                expires_at  TEXT,
                status      TEXT,   -- written | unknown
                identifier  TEXT,   -- NULL when status = unknown
                detail      TEXT
            )
        """)

    def connection(self) -> sqlite3.Connection:
        return self._conn
