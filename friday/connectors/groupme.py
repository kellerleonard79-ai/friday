"""
connectors/groupme.py
Read-only GroupMe connector. Polls each configured group's messages endpoint,
writes raw records to the events table. Never calls the LLM.
"""
import logging
import sqlite3
from datetime import datetime, timezone

import requests

logger = logging.getLogger("friday.groupme")

# Features: per-group polling with priority tiers (high/normal/muted) and an
# on/off switch, lazy name→id resolution, no-backfill first poll (cursor primed
# to newest), and a [priority=...] tag prepended to each body so the LLM's later
# urgency pass and event-extraction pass can weight the message correctly.
_API_BASE  = "https://api.groupme.com/v3"
_TIMEOUT_S = 15
_PAGE_SIZE = 100

# Process-lifetime cache of lowercased group name → group id. Populated lazily
# from GET /groups when a configured entry has a name but no id. Reset on restart.
_name_to_id: dict[str, str] = {}

# Canonical priority tiers — the single source of truth for the whole app. The
# dashboard UI writes exactly these three values; every consumer matches on the
# `[priority=<tier>]` tag this module prepends to each stored body.
#
#   high   → LLM urgency pass + event extraction + briefing (can interrupt)
#   normal → briefing only (never interrupts)
#   muted  → ingested for history, never surfaced
#
# 'low' is the pre-dashboard spelling. It normalizes to 'muted' because that is
# what it always did in practice: nothing downstream ever queried a non-'high'
# tag, so 'low' rows were stored and never shown.
PRIORITIES = ("high", "normal", "muted")
_PRIORITY_ALIASES = {"low": "muted"}
_DEFAULT_PRIORITY = "normal"


def normalize_priority(value: str | None, group_name: str = "") -> str:
    """Map any configured priority spelling onto a canonical tier.
    An unset priority defaults to 'normal'; an unrecognized one warns."""
    raw = (value or "").strip().lower()
    if not raw:
        return _DEFAULT_PRIORITY
    pri = _PRIORITY_ALIASES.get(raw, raw)
    if pri not in PRIORITIES:
        logger.warning(
            f"groupme[{group_name or '?'}] invalid priority {raw!r} — "
            f"using {_DEFAULT_PRIORITY!r}"
        )
        return _DEFAULT_PRIORITY
    return pri


def fetch(cfg: dict, conn: sqlite3.Connection) -> int:
    """Poll all configured GroupMe groups. Returns total new events written.
    Group entries may give an explicit id, or just a name (resolved against
    the account's group list)."""
    api_token = (cfg.get("api_token") or "").strip()
    groups    = cfg.get("groups") or []
    if not api_token:
        return 0

    # A group switched off in the dashboard is not polled at all. A missing
    # flag counts as enabled, matching the dashboard's own default.
    active   = [g for g in groups if g.get("enabled") is not False]
    disabled = [g for g in groups if g.get("enabled") is False]

    # Resolve any name-only entries up front so we make at most one
    # /groups call per poll cycle (and only if needed). Disabled groups are
    # excluded — we never poll them, so their ids aren't worth an API call.
    needed = {
        (g.get("name") or "").strip().lower()
        for g in active
        if not str(g.get("id") or "").strip()
           and (g.get("name") or "").strip()
    }
    if needed - set(_name_to_id):
        _populate_name_cache(api_token)

    for group in disabled:
        _forget_cursor(conn, group)

    total = 0
    for group in active:
        gid  = str(group.get("id") or "").strip()
        name = (group.get("name") or "").strip()
        if not name and not gid:
            continue  # placeholder row — silently skip
        if not gid:
            gid = _name_to_id.get(name.lower(), "")
            if not gid:
                logger.warning(
                    f"groupme: no group matching name {name!r} on this "
                    f"account — skipping"
                )
                continue
        if not name:
            name = gid  # fall back to id for log/title legibility
        pri = normalize_priority(group.get("priority"), name)
        try:
            total += _poll_one(api_token, gid, name, pri, conn)
        except Exception as e:
            logger.error(f"groupme[{name}] unexpected error: {e}")
    return total


def _populate_name_cache(api_token: str) -> None:
    """Fetch the account's group list and cache lowercased name → id.
    Failures are logged and swallowed — the per-group loop will warn for
    any name that doesn't resolve."""
    try:
        r = requests.get(
            f"{_API_BASE}/groups",
            params={"token": api_token, "per_page": 100},
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as e:
        logger.warning(f"groupme: name resolution fetch failed: {e}")
        return
    if r.status_code in (401, 403):
        logger.error(
            f"groupme: name resolution auth failed ({r.status_code}) — check api_token"
        )
        return
    if r.status_code != 200:
        logger.error(f"groupme: name resolution {r.status_code}: {r.text[:120]}")
        return
    try:
        all_groups = r.json().get("response") or []
    except ValueError as e:
        logger.error(f"groupme: name resolution bad JSON: {e}")
        return
    resolved = 0
    for g in all_groups:
        gname = (g.get("name") or "").strip().lower()
        gid   = str(g.get("id") or "").strip()
        if not gname or not gid:
            continue
        if gname in _name_to_id and _name_to_id[gname] != gid:
            logger.warning(
                f"groupme: duplicate name {gname!r} — keeping first "
                f"({_name_to_id[gname]}), ignoring {gid}"
            )
            continue
        if gname not in _name_to_id:
            _name_to_id[gname] = gid
            resolved += 1
    logger.info(
        f"groupme: name cache populated — {resolved} new, "
        f"{len(_name_to_id)} total cached."
    )


def _poll_one(api_token: str, gid: str, name: str, priority: str,
              conn: sqlite3.Connection) -> int:
    cursor_key = f"groupme_{gid}"
    row = conn.execute(
        "SELECT cursor FROM last_seen WHERE source = ?", (cursor_key,)
    ).fetchone()
    last_id = (row[0] if row else "") or ""
    now_iso = datetime.now(timezone.utc).isoformat()

    # First-poll path: no backfill. Probe the latest message, save its ID as
    # the cursor, write nothing. Same pattern as the Canvas migration choice.
    if not last_id:
        params = {"token": api_token, "limit": 1}
        msgs = _get_messages(gid, name, params, op="init")
        if msgs:
            init_cursor = str(msgs[0].get("id") or "").strip()
            if init_cursor:
                _set_cursor(conn, cursor_key, init_cursor, now_iso)
                logger.info(
                    f"groupme[{name}]: initial cursor set to {init_cursor} "
                    f"(no backfill of historical messages)"
                )
        return 0

    # Incremental path. since_id returns messages newer than `last_id`,
    # newest-first. Sort ascending so the highest ID lands last.
    params = {"token": api_token, "since_id": last_id, "limit": _PAGE_SIZE}
    msgs = _get_messages(gid, name, params, op="poll")
    if not msgs:
        return 0
    msgs.sort(key=lambda m: int(m.get("id", "0")))

    written = 0
    highest_id = last_id
    for m in msgs:
        try:
            mid = str(m.get("id") or "").strip()
            if not mid:
                continue
            if int(mid) > int(highest_id):
                highest_id = mid

            if m.get("system"):
                continue  # "Alice added Bob to the group" — skip
            sender = (m.get("name") or "").strip() or "(unknown)"
            text   = (m.get("text") or "").strip()
            attachments = m.get("attachments") or []
            if not text and not attachments:
                continue  # nothing actionable

            event_id = f"groupme_{mid}"
            if conn.execute(
                "SELECT 1 FROM events WHERE id = ?", (event_id,)
            ).fetchone():
                continue  # raced with another poll cycle

            body_lines = [
                f"[priority={priority}]",
                f"Group: {name}",
                f"From: {sender}",
            ]
            if text:
                body_lines.extend(["", text])
            if attachments:
                kinds = sorted({a.get("type", "?") for a in attachments})
                body_lines.extend([
                    "",
                    f"({len(attachments)} attachment(s): {', '.join(kinds)})",
                ])
            body = "\n".join(body_lines)[:2000]

            # calendar_synced=1 — GroupMe never writes to the calendar, so we
            # pre-flag these rows out of canvas.sync_to_calendar's query.
            # TODO: events table can produce duplicate rows for the same GroupMe
            # message (visible as repeated entries in the dashboard Today feed) —
            # fix at write time here (dedup on message id before INSERT).
            conn.execute(
                """INSERT INTO events
                   (id, source, title, body, due_at, urgency, processed,
                    notified, calendar_synced, created_at)
                   VALUES (?, 'groupme', ?, ?, NULL, NULL, 0, 0, 1, ?)""",
                (event_id, f"{name}: {sender}", body, now_iso),
            )
            written += 1
        except Exception as e:
            logger.error(f"groupme[{name}] message {m.get('id','?')} error: {e}")
            continue

    if written:
        conn.commit()
        logger.info(f"groupme[{name}]: {written} new message(s) written.")

    if highest_id != last_id:
        _set_cursor(conn, cursor_key, highest_id, now_iso)

    return written


def _get_messages(gid: str, name: str, params: dict, op: str) -> list[dict]:
    """Wrapper around the messages endpoint. Returns [] on any failure
    (304, network error, non-200, malformed JSON)."""
    try:
        r = requests.get(
            f"{_API_BASE}/groups/{gid}/messages",
            params=params, timeout=_TIMEOUT_S,
        )
    except requests.RequestException as e:
        logger.warning(f"groupme[{name}] {op} fetch failed: {e}")
        return []
    if r.status_code == 304:
        return []  # no new messages — normal idle response
    if r.status_code in (401, 403):
        logger.error(f"groupme[{name}] auth failed ({r.status_code}) — check api_token")
        return []
    if r.status_code != 200:
        logger.error(f"groupme[{name}] {op} {r.status_code}: {r.text[:120]}")
        return []
    try:
        return (r.json().get("response") or {}).get("messages") or []
    except ValueError as e:
        logger.error(f"groupme[{name}] {op} bad JSON: {e}")
        return []


def _forget_cursor(conn: sqlite3.Connection, group: dict) -> None:
    """Drop a disabled group's cursor so re-enabling it re-primes to the newest
    message (the same no-backfill path a brand-new group takes) instead of
    replaying everything missed while it was off. Uses only an id we already
    have — never triggers a name-resolution API call."""
    gid = str(group.get("id") or "").strip()
    if not gid:
        gid = _name_to_id.get((group.get("name") or "").strip().lower(), "")
    if not gid:
        return
    cur = conn.execute(
        "DELETE FROM last_seen WHERE source = ?", (f"groupme_{gid}",)
    )
    # rowcount is 0 on every cycle after the first, so this logs and commits
    # once per disable rather than every 15 minutes.
    if cur.rowcount:
        conn.commit()
        logger.info(
            f"groupme[{group.get('name') or gid}]: disabled — cursor cleared. "
            f"Re-enabling resumes from the newest message, with no backfill."
        )


def _set_cursor(conn: sqlite3.Connection, key: str, value: str, when_iso: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO last_seen (source, cursor, updated_at) "
        "VALUES (?, ?, ?)",
        (key, value, when_iso),
    )
    conn.commit()
