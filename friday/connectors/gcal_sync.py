"""
connectors/gcal_sync.py
Mirror Google Calendar iCal subscriptions into named Apple Calendars.
One-way (Google → Apple). Dedup via synced_events.google_event_id (iCal UID).
No LLM, no approval gate. Failures per calendar are logged and skipped.
"""
import logging
import sqlite3
from datetime import date, datetime, time, timezone

import requests
from icalendar import Calendar

from calendars import backend as cal_backend

logger = logging.getLogger("friday.gcal_sync")


def fetch(cfg: dict, conn: sqlite3.Connection) -> int:
    """Sync all configured Google calendars. Returns total new events written."""
    calendars = (cfg.get("gcal_sync") or {}).get("calendars") or []
    total = 0
    for entry in calendars:
        name = (entry.get("name") or "").strip()
        url  = (entry.get("ical_url") or "").strip()
        if not name or not url:
            continue
        try:
            total += _sync_one(name, url, conn)
        except Exception as e:
            logger.error(f"gcal_sync[{name}] unexpected error: {e}")
    return total


def _sync_one(name: str, url: str, conn: sqlite3.Connection) -> int:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"gcal_sync[{name}] fetch failed: {e}")
        return 0
    try:
        cal = Calendar.from_ical(r.content)
    except Exception as e:
        logger.error(f"gcal_sync[{name}] iCal parse error: {e}")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()
    count = 0
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        try:
            uid   = str(component.get("UID", "")).strip()
            title = str(component.get("SUMMARY", "")).strip()
            if not uid or not title:
                continue

            if conn.execute(
                "SELECT 1 FROM synced_events WHERE google_event_id = ?", (uid,)
            ).fetchone():
                continue

            dtstart = component.get("DTSTART")
            dtend   = component.get("DTEND")
            if dtstart is None:
                continue
            start_dt, end_dt, all_day = _resolve_times(dtstart, dtend)
            if start_dt is None:
                continue

            location    = str(component.get("LOCATION", "")).strip()
            description = str(component.get("DESCRIPTION", "")).strip()[:2000]

            # verify=False: a poll mirrors a whole feed, and a read-back per
            # event would cost more than the writes. The dedup here is
            # synced_events keyed on Google's UID, which is stronger than a
            # read-back anyway — a write we never recorded is retried next
            # poll and lands as a duplicate only if it actually succeeded,
            # which is the same exposure this loop already had.
            outcome = cal_backend.write_event(
                calendar_name=name, title=title,
                start=start_dt, end=end_dt,
                location=location, description=description,
                all_day=all_day, verify=False,
            )
            if not outcome.ok:
                continue  # writer logged; leave row out so next poll retries
            apple_uid = outcome.uid

            conn.execute(
                "INSERT INTO synced_events "
                "(google_event_id, calendar_name, apple_event_id, synced_at) "
                "VALUES (?, ?, ?, ?)",
                (uid, name, apple_uid, now_iso),
            )
            count += 1
        except Exception as e:
            logger.error(
                f"gcal_sync[{name}]: error on event "
                f"{component.get('UID','?')}: {e}"
            )
            continue

    if count:
        conn.commit()
        logger.info(f"gcal_sync[{name}]: {count} new event(s) written.")
    return count


def _resolve_times(dtstart, dtend):
    """iCal DTSTART may be timed (datetime) or all-day (date). Return
    (start_dt, end_dt, all_day). Returns (None, None, False) on shapes
    we can't handle."""
    start_val = dtstart.dt
    if isinstance(start_val, datetime):
        end_val = dtend.dt if dtend else start_val
        return start_val, end_val, False
    if isinstance(start_val, date):
        end_date = dtend.dt if dtend else start_val
        if isinstance(end_date, datetime):
            end_date = end_date.date()
        elif not isinstance(end_date, date):
            end_date = start_val
        return (datetime.combine(start_val, time.min),
                datetime.combine(end_date, time.min),
                True)
    return None, None, False
