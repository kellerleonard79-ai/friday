"""
calendars/google_cal.py
Google Calendar backend — reads and writes via the Google Calendar API.

Used on Windows, where Google Calendar replaces Apple Calendar as the event
store. Auth is the OAuth installed-app flow: the setup wizard performs the
initial browser consent and saves the token to paths.google_token_path();
this module only loads/refreshes that token. If the token is missing or
unrefreshable, reads return [] and writes return None — Friday never pops a
browser from the background process.

Synchronous — always wrap calls in run_in_executor inside async handlers.
"""

import logging
import time as time_mod
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import paths

logger = logging.getLogger("friday.googlecal")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

_service = None
_cal_map: dict[str, str] = {}   # calendar name -> calendarId
_cal_map_at: float = 0.0
_CAL_MAP_TTL_S = 600

# calendarList entries we never read from unless explicitly whitelisted
_DEFAULT_EXCLUDED_SUBSTRINGS = ("holiday", "#contacts", "#weeknum")


def reset() -> None:
    """Drop cached service + calendar map (used after re-auth)."""
    global _service, _cal_map, _cal_map_at
    _service = None
    _cal_map = {}
    _cal_map_at = 0.0


def load_credentials():
    """Load + refresh the saved OAuth token. Returns Credentials or None."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    token_path = paths.google_token_path()
    if not token_path.exists():
        logger.error(f"Google Calendar token missing: {token_path} — run setup.")
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as e:
        logger.error(f"Google Calendar token unreadable: {e}")
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            return creds
        except Exception as e:
            logger.error(f"Google Calendar token refresh failed: {e}")
            return None
    logger.error("Google Calendar credentials invalid — re-run setup.")
    return None


def _get_service():
    global _service
    if _service is not None:
        return _service
    creds = load_credentials()
    if creds is None:
        return None
    try:
        from googleapiclient.discovery import build
        _service = build("calendar", "v3", credentials=creds,
                         cache_discovery=False)
    except Exception as e:
        logger.error(f"Google Calendar service build failed: {e}")
        return None
    return _service


def _calendar_map(force: bool = False) -> dict[str, str]:
    """name -> calendarId for every calendar on the account. 'primary' is
    always present as a name. Cached for 10 minutes."""
    global _cal_map, _cal_map_at
    if not force and _cal_map and (time_mod.monotonic() - _cal_map_at) < _CAL_MAP_TTL_S:
        return _cal_map
    service = _get_service()
    if service is None:
        return _cal_map
    out: dict[str, str] = {}
    try:
        page_token = None
        while True:
            resp = service.calendarList().list(pageToken=page_token).execute()
            for item in resp.get("items", []):
                name = (item.get("summaryOverride") or item.get("summary") or "").strip()
                cal_id = item.get("id", "")
                if name and cal_id:
                    out[name] = cal_id
                if item.get("primary"):
                    out["primary"] = cal_id
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except Exception as e:
        logger.error(f"Google calendarList failed: {e}")
        return _cal_map
    _cal_map, _cal_map_at = out, time_mod.monotonic()
    return _cal_map


def calendar_exists(cfg: dict, name: str) -> bool:
    return name in _calendar_map()


def _ensure_calendar(cfg: dict, name: str) -> str | None:
    """Resolve a calendar name to its id, creating the calendar if allowed.
    Auto-create is on by default for the Google backend — secondary Google
    calendars are cheap and this keeps Canvas due dates organized without a
    manual step."""
    cmap = _calendar_map()
    if name in cmap:
        return cmap[name]
    auto_create = ((cfg.get("calendar") or {}).get("google") or {}).get("auto_create", True)
    if not auto_create:
        return None
    service = _get_service()
    if service is None:
        return None
    tz_name = (cfg.get("agent") or {}).get("timezone", "America/Chicago")
    try:
        created = service.calendars().insert(
            body={"summary": name, "timeZone": tz_name}
        ).execute()
        cal_id = created.get("id")
        logger.info(f"Google Calendar created: {name!r} ({cal_id})")
        _calendar_map(force=True)
        return cal_id
    except Exception as e:
        logger.error(f"Google Calendar create failed for {name!r}: {e}")
        return None


# ── Reads ─────────────────────────────────────────────────────────────────────

def _read_calendar_ids(cfg: dict) -> list[tuple[str, str]]:
    """(name, id) pairs to read from: the agent.briefing_calendars whitelist
    when set, otherwise every calendar except holiday/contact feeds."""
    cmap = _calendar_map()
    whitelist = (cfg.get("agent") or {}).get("briefing_calendars") or []
    if whitelist:
        pairs = [(n, cmap[n]) for n in whitelist if n in cmap]
        missing = [n for n in whitelist if n not in cmap]
        if missing:
            logger.debug(f"briefing_calendars not on account: {missing}")
        return pairs
    return [
        (n, cid) for n, cid in cmap.items()
        if n != "primary"
        and not any(s in cid.lower() for s in _DEFAULT_EXCLUDED_SUBSTRINGS)
    ]


def events_in_window(cfg: dict, start_date: date, end_date: date) -> list[dict]:
    """All events with start in [start_date 00:00 local, end_date 00:00 local).
    Same shape as the Apple backend: title/start_iso/end_iso/location/
    calendar/uid."""
    service = _get_service()
    if service is None:
        return []
    tz_name = (cfg.get("agent") or {}).get("timezone", "America/Chicago")
    local_tz = ZoneInfo(tz_name)
    time_min = datetime.combine(start_date, time.min, tzinfo=local_tz)
    time_max = datetime.combine(end_date, time.min, tzinfo=local_tz)

    out: list[dict] = []
    for name, cal_id in _read_calendar_ids(cfg):
        try:
            page_token = None
            while True:
                resp = service.events().list(
                    calendarId=cal_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=250,
                    pageToken=page_token,
                ).execute()
                for ev in resp.get("items", []):
                    start = ev.get("start") or {}
                    end   = ev.get("end") or {}
                    start_iso = start.get("dateTime") or start.get("date") or ""
                    end_iso   = end.get("dateTime") or end.get("date") or ""
                    if not start_iso:
                        continue
                    # The list query is half-open on event END time for events
                    # spanning the boundary; filter starts strictly into window.
                    try:
                        sdt = datetime.fromisoformat(start_iso)
                        if sdt.tzinfo is None:  # all-day 'date' value
                            sdt = sdt.replace(tzinfo=local_tz)
                        if not (time_min <= sdt < time_max):
                            continue
                    except ValueError:
                        pass
                    out.append({
                        "title":     ev.get("summary", "") or "",
                        "start_iso": start_iso,
                        "end_iso":   end_iso,
                        "location":  ev.get("location", "") or "",
                        "calendar":  name,
                        "uid":       ev.get("id", "") or "",
                    })
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except Exception as e:
            logger.error(f"Google Calendar read failed for {name!r}: {e}")
            continue
    out.sort(key=lambda e: e.get("start_iso", ""))
    return out


def events_for_day(cfg: dict, target_date: date) -> list[dict]:
    return events_in_window(cfg, target_date, target_date + timedelta(days=1))


# ── Writes ────────────────────────────────────────────────────────────────────

def write_event(cfg: dict, calendar_name: str, title: str, start: datetime,
                end: datetime, location: str = "", description: str = "",
                all_day: bool = False) -> str | None:
    """Create an event in the named Google Calendar. Returns the Google event
    id or None on failure. No dedup — caller's responsibility."""
    cal_id = _ensure_calendar(cfg, calendar_name)
    if cal_id is None:
        logger.error(f"Google Calendar write — no calendar {calendar_name!r}")
        return None
    service = _get_service()
    if service is None:
        return None
    tz_name = (cfg.get("agent") or {}).get("timezone", "America/Chicago")
    body: dict = {"summary": title}
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    if all_day:
        # Google all-day events take 'date' with an exclusive end date —
        # which matches the start + 1 day shape the callers already produce.
        body["start"] = {"date": start.date().isoformat()}
        end_date = end.date()
        if end_date <= start.date():
            end_date = start.date() + timedelta(days=1)
        body["end"] = {"date": end_date.isoformat()}
    else:
        # Naive datetimes are the user's local wall time; the API takes the
        # zone separately so no offset math is needed here.
        body["start"] = {"dateTime": start.isoformat(), "timeZone": tz_name}
        body["end"]   = {"dateTime": end.isoformat(),   "timeZone": tz_name}
    try:
        created = service.events().insert(calendarId=cal_id, body=body).execute()
        return created.get("id")
    except Exception as e:
        logger.error(f"Google Calendar write failed for {title!r}: {e}")
        return None


def update_event(cfg: dict, uid: str, calendar_name: str = "",
                 title: str | None = None, start: datetime | None = None,
                 end: datetime | None = None, location: str | None = None,
                 description: str | None = None,
                 all_day: bool | None = None) -> dict | None:
    """Modify an existing event, found by its Google event id. Only the fields
    passed as non-None are touched. Returns the event's post-update state, or
    None if it wasn't found / the write failed.

    Uses patch(), so unspecified fields are left alone server-side rather than
    being cleared. calendar_name narrows the lookup; without it every readable
    calendar is tried, since a Google event id is only unique within its own
    calendar's namespace.
    """
    service = _get_service()
    if service is None:
        return None
    tz_name = (cfg.get("agent") or {}).get("timezone", "America/Chicago")

    body: dict = {}
    if title is not None:
        body["summary"] = title
    if location is not None:
        body["location"] = location
    if description is not None:
        body["description"] = description
    if start is not None or end is not None:
        if all_day:
            if start is not None:
                body["start"] = {"date": start.date().isoformat()}
            if end is not None:
                end_date = end.date()
                if start is not None and end_date <= start.date():
                    end_date = start.date() + timedelta(days=1)
                body["end"] = {"date": end_date.isoformat()}
        else:
            if start is not None:
                body["start"] = {"dateTime": start.isoformat(), "timeZone": tz_name}
            if end is not None:
                body["end"] = {"dateTime": end.isoformat(), "timeZone": tz_name}
    if not body:
        logger.info(f"update_event — nothing to change for id {uid}")
        return None

    candidates: list[tuple[str, str]] = []
    if calendar_name:
        cal_id = _calendar_map().get(calendar_name)
        if cal_id:
            candidates.append((calendar_name, cal_id))
    candidates += [c for c in _read_calendar_ids(cfg) if c not in candidates]

    for name, cal_id in candidates:
        try:
            updated = service.events().patch(
                calendarId=cal_id, eventId=uid, body=body).execute()
        except Exception as e:
            # 404 here just means "not in this calendar" — keep looking.
            logger.debug(f"Google Calendar patch miss in {name!r}: {e}")
            continue
        start_raw = updated.get("start", {})
        end_raw   = updated.get("end", {})
        return {
            "uid":       updated.get("id", ""),
            "calendar":  name,
            "title":     updated.get("summary", ""),
            "start_iso": start_raw.get("dateTime") or start_raw.get("date", ""),
            "end_iso":   end_raw.get("dateTime") or end_raw.get("date", ""),
            "location":  updated.get("location", "") or "",
            "allDay":    "date" in start_raw,
        }
    logger.error(f"Google Calendar update — event {uid} not found in any calendar")
    return None
