"""
connectors/canvas.py
Read-only Canvas LMS connector. Fetches the iCal feed, deduplicates by UID,
and writes raw records to the events table. Never calls the LLM.
"""

import logging
import sqlite3
from datetime import datetime, timezone

import requests
from icalendar import Calendar

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
