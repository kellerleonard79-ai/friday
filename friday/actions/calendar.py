"""
actions/calendar.py
Write events to the user's calendar (Apple on macOS, Google on Windows —
see calendars/backend.py for the dispatch).

Two public modes:
    auto_write(event, telegram=None)    — write immediately, no approval gate.
    gated_write(event, conn, telegram)  — stage in pending_actions and prompt
                                          the user. confirm_pending() runs
                                          the write when ✅ is tapped.

Synchronous — always wrap calls in run_in_executor inside async handlers.

Canonical event dict:
    {
        "title":      str,
        "date":       "YYYY-MM-DD",
        "start_time": "HH:MM"   (optional — omit for all-day),
        "end_time":   "HH:MM"   (optional — defaults to start_time + 1h),
        "calendar":   str       (optional — falls back to default calendar),
        "notes":      str       (optional),
    }

Legacy "time" is still accepted as a synonym for start_time (back-compat).
"""
import json
import logging
import sqlite3
import uuid
from datetime import date as date_cls, datetime, timedelta

import compat
from calendars import backend as cal_backend

logger = logging.getLogger("friday.calendar.write")
_DEFAULT_DURATION = timedelta(hours=1)

# Back-compat alias — the low-level write moved to the backend dispatcher.
write_event = cal_backend.write_event


# The calendar app has no scriptable "default calendar" concept we can rely
# on, so the fallback name has to be supplied explicitly (typically
# agent.default_calendar in config).
def _resolve_calendar(requested: str | None, default: str | None) -> str | None:
    if requested and cal_backend.calendar_exists(requested):
        return requested
    if default and cal_backend.calendar_exists(default):
        if requested:
            logger.info(f"Calendar {requested!r} not found — using configured default {default!r}.")
        return default
    logger.error(
        f"No usable calendar — requested {requested!r}, default {default!r}."
    )
    return None


# ── Event payload helpers ─────────────────────────────────────────────────────

_TITLE_STOPWORDS = {"a", "an", "the", "and", "or", "but", "of", "in", "on",
                    "at", "to", "for", "with", "by", "vs", "via"}


def _normalize_title(title: str) -> str:
    """Backstop for the LLM's title-casing rule (see AGENTS.md): if the title
    is entirely lowercase or entirely uppercase, render it in Title Case.
    Mixed-case titles are left alone so deliberate casing like 'iPhone' or
    'FBLA' survives. Stopwords stay lowercase unless they're the first word."""
    if not title or any(c.isupper() for c in title) and any(c.islower() for c in title):
        return title
    words = title.split()
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if i > 0 and lw in _TITLE_STOPWORDS:
            out.append(lw)
        else:
            out.append(lw[:1].upper() + lw[1:])
    return " ".join(out)


def _parse_event(event: dict) -> tuple[str, datetime, datetime, bool]:
    """(title, start, end, all_day). Raises ValueError on bad input.
    Honors start_time/end_time as a single-day range; falls back to 1h
    duration when only start_time is given; treats no times as all-day.
    Overnight ranges (end <= start) are rejected for now."""
    title = _normalize_title((event.get("title") or "").strip())
    event["title"] = title
    if not title:
        raise ValueError("event.title required")
    date_str = (event.get("date") or "").strip()
    if not date_str:
        raise ValueError("event.date required (YYYY-MM-DD)")
    day = date_cls.fromisoformat(date_str)

    start_str = (event.get("start_time") or event.get("time") or "").strip()
    end_str   = (event.get("end_time") or "").strip()

    if not start_str:
        if end_str:
            raise ValueError("end_time given without start_time")
        start = datetime(day.year, day.month, day.day)
        return title, start, start + timedelta(days=1), True

    sh, sm = (int(x) for x in start_str.split(":", 1))
    start = datetime(day.year, day.month, day.day, sh, sm)
    if not end_str:
        return title, start, start + _DEFAULT_DURATION, False

    eh, em = (int(x) for x in end_str.split(":", 1))
    end = datetime(day.year, day.month, day.day, eh, em)
    if end <= start:
        raise ValueError(f"end_time {end_str} must be after start_time {start_str}")
    return title, start, end, False


def _friendly_date(date_str: str) -> str:
    try:
        return compat.strftime(date_cls.fromisoformat(date_str), "%A, %B %-d")
    except Exception:
        return date_str


def _friendly_date_phrase(date_str: str) -> str:
    """Spoken-form date for confirmations: 'today', 'tomorrow', or
    'on Tuesday, June 3' for anything further out."""
    try:
        d = date_cls.fromisoformat(date_str)
    except ValueError:
        return date_str
    today = date_cls.today()
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return compat.strftime(d, "on %A, %B %-d")


def _confirmation_date(date_str: str) -> str:
    """'today' / 'tomorrow' / 'Thursday, June 18' — bare, no preposition."""
    try:
        d = date_cls.fromisoformat(date_str)
    except ValueError:
        return date_str
    today = date_cls.today()
    if d == today:
        return "today"
    if d == today + timedelta(days=1):
        return "tomorrow"
    return compat.strftime(d, "%A, %B %-d")


def _friendly_time(time_str: str) -> str:
    """24h 'HH:MM' → '8:00 AM' / '12:00 PM'. Returns the input on parse failure."""
    if not time_str:
        return ""
    try:
        h, m = (int(x) for x in time_str.split(":", 1))
    except ValueError:
        return time_str
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {period}"


def format_confirmation(event: dict, quip: str = "") -> str:
    """One-line confirmation Friday sends after a successful add. Public so
    callers can store the exact text in conversation_history without rebuilding
    it — past history rows shape the LLM's future output, so the row must
    match what the user actually saw."""
    title = (event.get("title") or "").strip()
    when_phrase = _confirmation_date(event.get("date", ""))
    start = (event.get("start_time") or event.get("time") or "").strip()
    time_phrase = f" at {_friendly_time(start)}" if start else ""
    msg = f"{title} added for {when_phrase}{time_phrase}."
    if quip:
        msg = f"{msg} {quip}"
    return msg


def _format_when(event: dict) -> str:
    """Render the time component for a draft card."""
    start = (event.get("start_time") or event.get("time") or "").strip()
    end   = (event.get("end_time") or "").strip()
    if start and end:
        return f" {start}–{end}"
    if start:
        return f" at {start}"
    return " (all day)"


# ── Public API ────────────────────────────────────────────────────────────────

def auto_write(event: dict, telegram=None, default_calendar: str | None = None,
               quip: str = "") -> str | None:
    """Immediate write, no approval gate. Returns Apple UID or None.
    Sends a Telegram confirmation/failure note only if telegram is provided.
    default_calendar is the fallback when event['calendar'] is missing or
    doesn't exist on the system. quip, if provided, is appended to the
    success confirmation so personality ships in the same message."""
    try:
        title, start, end, all_day = _parse_event(event)
    except ValueError as e:
        logger.error(f"auto_write — bad event: {e}")
        if telegram:
            telegram.send(f"Couldn't add event, sir — {e}.")
        return None

    cal = _resolve_calendar(event.get("calendar"), default_calendar)
    if not cal:
        logger.error("auto_write — no usable calendar available")
        if telegram:
            telegram.send(f"Couldn't add {title!r}, sir — no calendar available.")
        return None

    notes = (event.get("notes") or "").strip()
    try:
        uid = write_event(cal, title, start, end,
                          description=notes, all_day=all_day)
    except Exception as e:
        logger.exception(f"auto_write — unexpected: {e}")
        if telegram:
            telegram.send(f"Calendar write failed for {title!r}, sir.")
        return None

    if not uid:
        if telegram:
            telegram.send(f"Calendar write failed for {title!r}, sir.")
        return None

    if telegram:
        telegram.send(format_confirmation(event, quip))
    return uid


def gated_write(event: dict, conn: sqlite3.Connection, telegram,
                default_calendar: str | None = None) -> str | None:
    """Stage event in pending_actions and prompt the user. The write happens
    only after confirm_pending() runs. Returns the pending key, or None if
    the event was rejected upfront. The fallback calendar is resolved now so
    the prompt shows the actual calendar that'll be written."""
    try:
        title, _, _, _ = _parse_event(event)
    except ValueError as e:
        logger.error(f"gated_write — bad event: {e}")
        telegram.send(f"Couldn't draft that event, sir — {e}.")
        return None

    resolved = _resolve_calendar(event.get("calendar"), default_calendar)
    if not resolved:
        telegram.send(f"Couldn't draft {title!r}, sir — no calendar available.")
        return None
    event = dict(event)
    event["calendar"] = resolved  # pin so confirm_pending writes to the same place

    pending_key = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO pending_actions (id, action_type, payload, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (pending_key, "calendar_add", json.dumps(event), "pending",
         datetime.now().isoformat()),
    )
    conn.commit()

    when     = _friendly_date(event.get("date", ""))
    cal_name = (event.get("calendar") or "default") or "default"
    notes    = (event.get("notes") or "").strip()
    draft = (
        f"Add to calendar?\n\n"
        f"📌 {title}\n"
        f"📅 {when}{_format_when(event)}\n"
        f"🗂  {cal_name}"
    )
    if notes:
        draft += f"\n📝 {notes}"
    telegram.send_permission_request(draft, pending_key)
    return pending_key


def confirm_pending(pending_key: str, conn: sqlite3.Connection, telegram) -> bool:
    """Execute a pending calendar_add. Returns True on successful write."""
    row = conn.execute(
        "SELECT action_type, payload, status FROM pending_actions WHERE id = ?",
        (pending_key,),
    ).fetchone()
    if not row:
        logger.warning(f"confirm_pending — unknown key {pending_key}")
        return False
    action_type, payload, status = row
    if action_type != "calendar_add":
        logger.warning(f"confirm_pending — wrong type {action_type}")
        return False
    if status != "pending":
        logger.info(f"confirm_pending — already {status}, ignoring")
        return False
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        logger.error(f"confirm_pending — bad payload for {pending_key}")
        return False

    uid = auto_write(event, telegram=telegram)
    conn.execute(
        "UPDATE pending_actions SET status = ? WHERE id = ?",
        ("confirmed" if uid else "failed", pending_key),
    )
    conn.commit()
    return bool(uid)


def cancel_pending(pending_key: str, conn: sqlite3.Connection, telegram) -> None:
    """Mark a pending calendar_add as cancelled and notify the user."""
    row = conn.execute(
        "SELECT payload, status FROM pending_actions WHERE id = ?", (pending_key,),
    ).fetchone()
    if not row:
        return
    payload, status = row
    if status != "pending":
        return
    conn.execute(
        "UPDATE pending_actions SET status = 'cancelled' WHERE id = ?",
        (pending_key,),
    )
    conn.commit()
    try:
        title = json.loads(payload).get("title", "event")
    except json.JSONDecodeError:
        title = "event"
    telegram.send(f"Cancelled — {title} not added, sir.")
