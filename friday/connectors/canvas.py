"""
connectors/canvas.py
Read-only Canvas LMS connector. Fetches the iCal feed, deduplicates by UID,
and writes raw records to the events table. Never calls the LLM.
"""

import logging
import sqlite3
from datetime import date as date_cls, datetime, time, timezone
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from actions import calendar as apple_writer

logger = logging.getLogger("friday.canvas")


def fetch(cfg: dict, conn: sqlite3.Connection) -> int:
    """
    Fetch new Canvas events from the iCal feed and write them to the events table.
    Returns the count of new events written.
    """
    ical_url  = cfg.get("ical_url", "").strip()
    api_token = cfg.get("api_token", "").strip()

    if not ical_url:
        return 0

    # Fetch iCal feed
    headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
    try:
        r = requests.get(ical_url, headers=headers, timeout=30)
        if r.status_code == 401 or r.status_code == 403:
            logger.error(f"Canvas auth failed ({r.status_code}). Check api_token in config.")
            return 0
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"Canvas fetch failed: {e}")
        return 0

    # Parse iCal
    try:
        cal = Calendar.from_ical(r.content)
    except Exception as e:
        logger.error(f"Canvas iCal parse error: {e}")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    count   = 0

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        try:
            uid     = str(component.get("UID", "")).strip()
            title   = str(component.get("SUMMARY", "")).strip()
            body    = str(component.get("DESCRIPTION", "")).strip()[:2000]
            dtstart = component.get("DTSTART")

            if not uid or not title:
                continue

            due_at = None
            if dtstart:
                dt = dtstart.dt
                if hasattr(dt, "isoformat"):
                    due_at = dt.isoformat()

            event_id = f"canvas_{uid}"

            # Deduplicate — iCal feeds return the full calendar every poll
            exists = conn.execute(
                "SELECT 1 FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if exists:
                continue

            conn.execute(
                """INSERT INTO events
                   (id, source, title, body, due_at, urgency, processed, notified, created_at)
                   VALUES (?, ?, ?, ?, ?, NULL, 0, 0, ?)""",
                (event_id, "canvas", title, body, due_at, now_iso),
            )
            count += 1

        except Exception as e:
            logger.error(f"Canvas: error processing event {component.get('UID', '?')}: {e}")
            continue

    if count:
        conn.commit()

    # Update last_seen cursor
    conn.execute(
        "INSERT OR REPLACE INTO last_seen (source, cursor, updated_at) VALUES (?, ?, ?)",
        ("canvas", now_iso, now_iso),
    )
    conn.commit()

    return count


def sync_to_apple_calendar(config: dict, conn: sqlite3.Connection) -> int:
    """Write unsynced Canvas events with a future due_at to the 'Canvas'
    Apple Calendar (auto_write handles the default-calendar fallback).
    Idempotent via events.calendar_synced. Returns count of new writes."""
    agent_cfg = config.get("agent") or {}
    tz_name   = agent_cfg.get("timezone", "America/Chicago")
    default_cal = agent_cfg.get("default_calendar")
    local_tz  = ZoneInfo(tz_name)
    now_local = datetime.now(local_tz)

    rows = conn.execute(
        "SELECT id, title, body, due_at FROM events "
        "WHERE source='canvas' AND due_at IS NOT NULL AND calendar_synced = 0"
    ).fetchall()

    written = 0
    for event_id, title, body, due_at in rows:
        try:
            date_str, time_str, due_dt_local = _due_at_local(due_at, local_tz)
        except Exception as e:
            logger.error(f"canvas sync — bad due_at {due_at!r}: {e}")
            # Unparseable — mark synced so we don't retry forever.
            conn.execute(
                "UPDATE events SET calendar_synced = 1 WHERE id = ?", (event_id,),
            )
            continue
        if due_dt_local < now_local:
            conn.execute(
                "UPDATE events SET calendar_synced = 1 WHERE id = ?", (event_id,),
            )
            continue
        event = {
            "title":    title,
            "date":     date_str,
            "calendar": "Canvas",
            "notes":    (body or "")[:1000],
        }
        if time_str:
            event["time"] = time_str
        uid = apple_writer.auto_write(event, default_calendar=default_cal)  # silent
        if uid:
            conn.execute(
                "UPDATE events SET calendar_synced = 1 WHERE id = ?", (event_id,),
            )
            written += 1
        # else: leave flag at 0; next 15-min poll retries.
    if rows:
        conn.commit()
    return written


def _due_at_local(due_at_iso: str, tz: ZoneInfo) -> tuple[str, str | None, datetime]:
    """(YYYY-MM-DD, HH:MM or None, local-aware datetime). All-day inputs
    (no 'T') stay all-day; their comparison datetime is midnight local."""
    if "T" not in due_at_iso:
        d = date_cls.fromisoformat(due_at_iso)
        return due_at_iso[:10], None, datetime.combine(d, time.min, tzinfo=tz)
    dt = datetime.fromisoformat(due_at_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    dt_local = dt.astimezone(tz)
    return dt_local.strftime("%Y-%m-%d"), dt_local.strftime("%H:%M"), dt_local
