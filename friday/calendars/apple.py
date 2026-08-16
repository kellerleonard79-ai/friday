"""
calendars/apple.py
Apple Calendar backend — reads and writes via JXA (osascript -l JavaScript).
macOS only. Reads are delegated to connectors/apple_calendar.py (unchanged);
the low-level write/exists JXA lives here so calendars/backend.py is the one
write path for every platform.

Synchronous — always wrap calls in run_in_executor inside async handlers.
"""

import json
import logging
import subprocess
from datetime import date, datetime

from calendars import writes
from connectors.apple_calendar import events_for_day, events_in_window  # noqa: F401

logger = logging.getLogger("friday.applecal.write")
_TIMEOUT_S = 15
# Updates get a longer leash than creates: locating the event can fall back to
# a whole-calendar uid scan, which alone measured ~11 s (see update_event).
_UPDATE_TIMEOUT_S = 45


def write_event(cfg: dict, calendar_name: str, title: str, start: datetime,
                end: datetime, location: str = "", description: str = "",
                all_day: bool = False) -> writes.WriteOutcome:
    """Create an event in the named Apple Calendar. No dedup — the caller's
    job (see actions/calendar.py and the recent_writes fingerprint).

    Returns a WriteOutcome, not a UID. The distinction that matters is between
    a JXA error — the script ran and told us the calendar does not exist, so
    nothing happened — and a timeout or a non-zero exit, where osascript
    stopped talking to us and the push may already have reached Calendar.app.
    Both used to return None. See calendars/writes.py."""
    payload = {
        "calendar":    calendar_name,
        "summary":     title,
        "location":    location,
        "description": description,
        "allDay":      bool(all_day),
    }
    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(end.timestamp() * 1000)
    script = f"""
const Calendar = Application('Calendar');
const data = {json.dumps(payload)};
const targets = Calendar.calendars.whose({{name: data.calendar}})();
if (targets.length === 0) {{
  JSON.stringify({{error: "calendar not found: " + data.calendar}});
}} else {{
  try {{
    const e = Calendar.Event({{
      summary:     data.summary,
      startDate:   new Date({start_ms}),
      endDate:     new Date({end_ms}),
      location:    data.location,
      description: data.description,
      alldayEvent: data.allDay
    }});
    targets[0].events.push(e);
    JSON.stringify({{uid: e.uid()}});
  }} catch (err) {{
    JSON.stringify({{error: err.toString()}});
  }}
}}
"""
    # Each failure below is classified by WHAT WE KNOW, not by how bad it is.
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # The push was already issued when we stopped waiting. Calendar.app may
        # well have committed it. This is the case a blind retry double-books.
        logger.warning(f"Apple Calendar write timed out for {title!r}")
        return writes.unknown(f"osascript timed out after {_TIMEOUT_S}s")
    if result.returncode != 0:
        # osascript died rather than answering. Where it died relative to the
        # push is not knowable from here.
        detail = result.stderr.strip()[:200]
        logger.error(f"Apple Calendar write failed: {detail}")
        return writes.unknown(f"osascript exited {result.returncode}: {detail}")
    try:
        out = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        logger.error(f"Apple Calendar write — bad JSON: {result.stdout[:200]}")
        return writes.unknown(f"unparseable osascript output: {result.stdout[:200]}")
    if "error" in out:
        # The script ran to completion and reported a reason. Nothing happened.
        logger.error(f"Apple Calendar write error: {out['error']}")
        return writes.refused(str(out["error"]))
    uid = out.get("uid")
    if not uid:
        # Ran, no error, no identifier. Something was created that we cannot
        # name, which is the definition of unknown.
        logger.error(f"Apple Calendar write returned no uid for {title!r}")
        return writes.unknown("the write reported success but returned no uid")
    return writes.written(str(uid))


def update_event(cfg: dict, uid: str, calendar_name: str = "",
                 title: str | None = None, start: datetime | None = None,
                 end: datetime | None = None, location: str | None = None,
                 description: str | None = None,
                 all_day: bool | None = None) -> dict | None:
    """Modify an existing event, found by its Apple UID. Only the fields passed
    as non-None are touched; everything else keeps its current value. Returns
    the event's post-update state, or None if it wasn't found / the write
    failed.

    Finding the event is the expensive half: `whose()` can't use an index, so
    it walks every event in the calendar. Measured on the user's ~2.6k-event
    calendar, matching on uid costs ~12.8 s against ~0.3 s for the property
    write itself. Pre-filtering on a startDate window instead was tried and is
    *slower* (~14.1 s) — evaluating two date comparisons per event beats one
    string equality — so don't reintroduce it. The real fix is granting
    EventKit access, which drops reads to milliseconds; see the reader in
    connectors/apple_calendar.py.

    `calendar_name` is what actually helps, keeping the walk to one calendar
    instead of all of them. It's a hint, not a filter: on a miss the script
    falls back to scanning every calendar rather than reporting a spurious
    not-found, since the reader's `calendar` field can lag an event that was
    moved between calendars.
    """
    changes: dict = {}
    if title is not None:
        changes["summary"] = title
    if location is not None:
        changes["location"] = location
    if description is not None:
        changes["description"] = description
    if all_day is not None:
        changes["allDay"] = bool(all_day)
    if start is not None:
        changes["startMs"] = int(start.timestamp() * 1000)
    if end is not None:
        changes["endMs"] = int(end.timestamp() * 1000)
    if not changes:
        logger.info(f"update_event — nothing to change for uid {uid}")
        return None

    req = {"uid": uid, "calendar": calendar_name or "", "changes": changes}
    script = f"""
const Calendar = Application('Calendar');
const req = {json.dumps(req)};
function findByUid(cals) {{
  for (const c of cals) {{
    const m = c.events.whose({{uid: req.uid}})();
    if (m.length > 0) return {{event: m[0], cal: c.name()}};
  }}
  return null;
}}
const named = req.calendar ? Calendar.calendars.whose({{name: req.calendar}})() : [];
let hit = null;
if (named.length) hit = findByUid(named);
if (!hit) hit = findByUid(Calendar.calendars());
if (!hit) {{
  JSON.stringify({{error: "event not found: " + req.uid}});
}} else {{
  try {{
    const e = hit.event, ch = req.changes;
    // ORDER MATTERS, AND NEITHER FIXED ORDER IS SAFE. Calendar.app validates
    // start < end on EACH property assignment, not just at save. Setting
    // startDate first breaks when the new start lands after the event's
    // CURRENT end (moving the whole event later) — the assignment throws
    // "The start date must be before the end date." with the new start and
    // the not-yet-updated end. Setting endDate first breaks symmetrically
    // when the new end lands before the event's CURRENT start (moving it
    // earlier). So when both are changing, assign whichever bound moves
    // OUTWARD first — away from the other one's current value — so the
    // instant between the two assignments always has start < end.
    if ('startMs' in ch && 'endMs' in ch) {{
      const newStart = new Date(ch.startMs), newEnd = new Date(ch.endMs);
      if (newStart > e.endDate()) {{ e.endDate = newEnd; e.startDate = newStart; }}
      else                        {{ e.startDate = newStart; e.endDate = newEnd; }}
    }} else {{
      if ('startMs' in ch) e.startDate = new Date(ch.startMs);
      if ('endMs' in ch)   e.endDate   = new Date(ch.endMs);
    }}
    if ('summary' in ch)      e.summary     = ch.summary;
    if ('location' in ch)     e.location    = ch.location;
    if ('description' in ch)  e.description = ch.description;
    if ('allDay' in ch)       e.alldayEvent = ch.allDay;
    JSON.stringify({{
      uid:       e.uid(),
      calendar:  hit.cal,
      title:     e.summary(),
      start_iso: e.startDate().toISOString(),
      end_iso:   e.endDate().toISOString(),
      location:  (e.location() || "").toString(),
      allDay:    e.alldayEvent()
    }});
  }} catch (err) {{
    JSON.stringify({{error: err.toString()}});
  }}
}}
"""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_UPDATE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"Apple Calendar update timed out for uid {uid}")
        return None
    if result.returncode != 0:
        logger.error(f"Apple Calendar update failed: {result.stderr.strip()[:200]}")
        return None
    try:
        out = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        logger.error(f"Apple Calendar update — bad JSON: {result.stdout[:200]}")
        return None
    if "error" in out:
        logger.error(f"Apple Calendar update error: {out['error']}")
        return None
    return out


def delete_event(cfg: dict, uid: str, calendar_name: str = "") -> writes.WriteOutcome:
    """Delete an event, found by its Apple UID. Returns a WriteOutcome — see
    calendars/writes.py. `calendar_name` narrows the JXA scan the same way
    update_event's does; omit it and every calendar is tried.

    "Not found" and "found but the delete itself errored" both come back
    refused: either way nothing was removed, so a retry is safe. A timeout or
    a non-zero exit is unknown for the same reason write_event's is — the
    request may already have reached Calendar.app.
    """
    req = {"uid": uid, "calendar": calendar_name or ""}
    script = f"""
const Calendar = Application('Calendar');
const req = {json.dumps(req)};
function findByUid(cals) {{
  for (const c of cals) {{
    const m = c.events.whose({{uid: req.uid}})();
    if (m.length > 0) return m[0];
  }}
  return null;
}}
const named = req.calendar ? Calendar.calendars.whose({{name: req.calendar}})() : [];
let hit = named.length ? findByUid(named) : null;
if (!hit) hit = findByUid(Calendar.calendars());
if (!hit) {{
  JSON.stringify({{error: "event not found: " + req.uid}});
}} else {{
  try {{
    Calendar.delete(hit);
    JSON.stringify({{deleted: req.uid}});
  }} catch (err) {{
    JSON.stringify({{error: err.toString()}});
  }}
}}
"""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_UPDATE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"Apple Calendar delete timed out for uid {uid}")
        return writes.unknown(f"osascript timed out after {_UPDATE_TIMEOUT_S}s")
    if result.returncode != 0:
        detail = result.stderr.strip()[:200]
        logger.error(f"Apple Calendar delete failed: {detail}")
        return writes.unknown(f"osascript exited {result.returncode}: {detail}")
    try:
        out = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        logger.error(f"Apple Calendar delete — bad JSON: {result.stdout[:200]}")
        return writes.unknown(f"unparseable osascript output: {result.stdout[:200]}")
    if "error" in out:
        logger.error(f"Apple Calendar delete error: {out['error']}")
        return writes.refused(str(out["error"]))
    return writes.written(str(uid))


# Bounded hard. This runs while the user is waiting on a confirmation, and on
# a very large calendar the uid scan is slow enough (~12.8s measured on 2,600
# events — see update_event) that waiting for it would be worse than answering
# "unconfirmed". A timeout here is not an error: it is the honest state.
_EXISTS_TIMEOUT_S = 6


def event_exists(cfg: dict, calendar_name: str, uid: str) -> bool:
    """Whether Calendar.app holds `uid` in `calendar_name`.

    ASKS THE AUTHORITY THAT ACCEPTED THE WRITE. The EventKit reader is a
    different view of the same database and lags a JXA write by long enough
    that a read-back a second later reliably misses it — so verifying a JXA
    write through EventKit means asking a component that has not heard about it
    yet. This asks Calendar.app, which has.

    Scoped to one named calendar, which is what keeps it affordable: the cost
    of a uid scan is per event IN THE CALENDAR SCANNED, and the write path
    always knows which calendar it just wrote to.

    False on timeout, on failure, and on genuinely-not-there alike. The caller
    reports it as "written but unconfirmed" and does not downgrade the write,
    so conflating them costs nothing — see calendars/backend.py.
    """
    if not uid or not calendar_name:
        return False
    script = f"""
const Calendar = Application('Calendar');
const cals = Calendar.calendars.whose({{name: {json.dumps(calendar_name)}}})();
let found = false;
for (const c of cals) {{
  if (c.events.whose({{uid: {json.dumps(uid)}}})().length > 0) {{ found = true; break; }}
}}
JSON.stringify({{exists: found}});
"""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_EXISTS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.info(
            f"Read-back of {uid} in {calendar_name!r} exceeded "
            f"{_EXISTS_TIMEOUT_S}s; reporting the write as unconfirmed.")
        return False
    if result.returncode != 0:
        return False
    try:
        return bool(json.loads(result.stdout.strip()).get("exists"))
    except json.JSONDecodeError:
        return False


def _run_jxa(script: str) -> dict | None:
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return None


def list_calendars_detailed(cfg: dict | None = None) -> list[tuple[str, bool]]:
    """Every calendar Calendar.app knows about, as (name, writable) pairs.

    Used by the setup wizard to populate its pickers. The first call is what
    triggers macOS's Automation permission prompt, so an empty list here
    usually means the user denied it rather than that they have no calendars.

    Writability matters: subscribed calendars (holidays, a shared read-only
    feed) show up alongside real ones, and picking one as the default calendar
    makes every later write_event fail. One JXA round trip returns both lists
    so the wizard doesn't pay for the permission-checked bridge twice.
    """
    out = _run_jxa("""
const Calendar = Application('Calendar');
JSON.stringify({
  names:    Calendar.calendars.name(),
  writable: Calendar.calendars.writable()
});
""") or {}
    names = out.get("names", []) or []
    writable = out.get("writable", []) or []
    return [(n, bool(writable[i]) if i < len(writable) else True)
            for i, n in enumerate(names) if n]


def calendar_exists(cfg: dict, name: str) -> bool:
    out = _run_jxa(f"""
const Calendar = Application('Calendar');
const t = Calendar.calendars.whose({{name: {json.dumps(name)}}})();
JSON.stringify({{exists: t.length > 0}});
""")
    return bool((out or {}).get("exists"))
