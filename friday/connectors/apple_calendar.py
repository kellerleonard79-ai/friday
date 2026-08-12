"""
connectors/apple_calendar.py
Read-only Apple Calendar reader. Returns events for a date or window, filtered
by a config whitelist of calendar names.

Two backends, tried in order:

  1. EventKit (PyObjC) — queries the local calendar store directly. Constant
     time, milliseconds.
  2. JXA via osascript — the original path, kept as a fallback for when
     EventKit is missing or its TCC grant was refused.

They are not interchangeable on speed. Every event property read over the
Apple Events bridge is a separate IPC round trip, so JXA costs roughly 35 ms
per event *in the calendar being scanned*, not per event returned: a shared
"Family" calendar with 2,600 events took 55 s to answer "what is on today",
and a whole briefing bundle (today + tomorrow + the week) took over six
minutes and hit every timeout. Bulk property fetches (`cal.events.startDate()`)
are not a workaround — measured slower still. EventKit answers the same query
in 3 ms because it reads the store instead of talking to Calendar.app.

That is why the timeout below is generous: it only applies to the fallback,
where a slow answer still beats an empty briefing.

Synchronous — always wrap calls in run_in_executor inside async handlers.
"""

import json
import logging
import subprocess
import threading
from datetime import date, datetime, timedelta

logger = logging.getLogger("friday.applecal")

_DEFAULT_EXCLUDED = ("Siri Suggestions", "US Holidays")
# Fallback-only. See the module docstring: a real calendar can genuinely need
# a minute over the Apple Events bridge, and the 15 s this used to be turned
# every briefing on a busy calendar into a silent "nothing scheduled".
_TIMEOUT_S = 150


# ── EventKit fast path ───────────────────────────────────────────────────────

_ek_lock = threading.Lock()
_ek_store = None          # cached EKEventStore once authorized
_ek_missing = False       # sticky: the framework itself isn't importable
_ek_requested = False     # the interactive prompt has been shown once


def _eventkit_store():
    """The authorized EKEventStore, or None to fall back to JXA.

    Two kinds of "no", handled differently. A missing framework is permanent,
    so it latches. A refused or unanswered *permission* does not: the user can
    grant access in System Settings at any time, and a long-lived core process
    must pick that up without being restarted. So the (instant, non-prompting)
    status check runs on every call, while the prompt itself is issued once.
    """
    global _ek_store, _ek_missing, _ek_requested
    with _ek_lock:
        if _ek_store is not None:
            return _ek_store
        if _ek_missing:
            return None
        try:
            import EventKit
            import Foundation
        except ImportError as e:
            logger.info(f"EventKit unavailable ({e}); using the JXA reader.")
            _ek_missing = True
            return None

        try:
            store = EventKit.EKEventStore.alloc().init()
            status = EventKit.EKEventStore.authorizationStatusForEntityType_(
                EventKit.EKEntityTypeEvent)
            # 3 is both EKAuthorizationStatusAuthorized and, on macOS 14+,
            # EKAuthorizationStatusFullAccess. Anything else (including 4 =
            # write-only, which reads as "granted" but returns no events) has
            # to go through a request first.
            if status != 3:
                if _ek_requested:
                    # Already asked once this process; don't re-prompt on every
                    # briefing. The status check above still runs each time, so
                    # a grant made in System Settings is picked up immediately.
                    return None
                _ek_requested = True
                done = {}

                def _completion(granted, err) -> None:
                    # Must return None. PyObjC type-checks the block signature
                    # against the ObjC void return and raises inside the
                    # callback thread otherwise — an uncaught NSException that
                    # aborts the whole process, not something we could catch
                    # here. A bare `lambda g, e: done.setdefault(...)` returns
                    # the dict value and does exactly that.
                    done["granted"] = bool(granted)
                    if err is not None:
                        done["error"] = str(err)

                if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
                    # macOS 14+. The older call silently yields write-only here.
                    store.requestFullAccessToEventsWithCompletion_(_completion)
                else:
                    store.requestAccessToEntityType_completion_(
                        EventKit.EKEntityTypeEvent, _completion)
                # The completion handler is delivered via the run loop, so this
                # thread has to pump it. Bounded — a prompt the user never
                # answers must not wedge a briefing.
                loop = Foundation.NSRunLoop.currentRunLoop()
                deadline = datetime.now() + timedelta(seconds=30)
                while "granted" not in done and datetime.now() < deadline:
                    loop.runMode_beforeDate_(
                        Foundation.NSDefaultRunLoopMode,
                        Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05))
                if not done.get("granted"):
                    logger.warning(
                        "EventKit access not granted (status %s); falling back "
                        "to the slower JXA reader. Grant Friday full Calendar "
                        "access in System Settings → Privacy & Security → "
                        "Calendars to speed briefings up — it is picked up "
                        "without a restart.", status)
                    return None
        except Exception as e:
            logger.warning(f"EventKit init failed ({e}); using the JXA reader.")
            _ek_missing = True
            return None

        _ek_store = store
        logger.info("Apple Calendar reads using EventKit.")
        return _ek_store


def refresh() -> bool:
    """Pull the EventKit store up to date with writes made outside it.

    THE PROBLEM THIS SOLVES. Writes go out through JXA (calendars/apple.py) and
    reads come back through EventKit. Those are two different views of the same
    database, and EKEventStore serves a cached snapshot that is refreshed when
    it processes an EKEventStoreChanged notification — which has not happened
    yet twenty milliseconds after a write. So a write's own read-back looked
    for the event it had just created and did not find it, and reported a real
    write as unconfirmed.

    Returns True if a refresh was performed. False on the JXA path, where there
    is no cached store and nothing to refresh — a JXA read talks to
    Calendar.app, which already knows.

    Cheap: reset() drops cached objects, it does not re-authorize. The
    authorization check is cached separately in _eventkit_store().
    """
    store = _eventkit_store()
    if store is None:
        return False
    try:
        # reset(), not refreshSourcesIfNecessary(). The latter refreshes REMOTE
        # sources — CalDAV, Exchange — and does nothing about the store's own
        # in-memory caches, which is where a JXA write is invisible. reset() is
        # the documented response to an EKEventStoreChanged notification and is
        # what actually invalidates them. Tried the other one first; the
        # read-back still missed every write.
        store.reset()
        return True
    except Exception as e:
        logger.debug(f"EventKit refresh failed: {e}")
        return False


def _eventkit_events(start: datetime, end: datetime) -> list[dict] | None:
    """Events starting in [start, end), or None if EventKit can't serve it."""
    store = _eventkit_store()
    if store is None:
        return None
    try:
        import EventKit  # noqa: F401  (proves the import still works)
        import Foundation

        ns_start = Foundation.NSDate.dateWithTimeIntervalSince1970_(start.timestamp())
        ns_end   = Foundation.NSDate.dateWithTimeIntervalSince1970_(end.timestamp())
        pred = store.predicateForEventsWithStartDate_endDate_calendars_(
            ns_start, ns_end, None)
        out = []
        for e in store.eventsMatchingPredicate_(pred) or []:
            # The predicate matches events *overlapping* the window; the JXA
            # reader (and every caller) means events that START in it. Filter
            # so a multi-day event doesn't show up on every one of its days.
            start_dt = _ns_to_datetime(e.startDate())
            if start_dt is None or not (start <= start_dt < end):
                continue
            end_dt = _ns_to_datetime(e.endDate())
            out.append({
                "title":     str(e.title() or ""),
                "start_iso": start_dt.isoformat(),
                "end_iso":   end_dt.isoformat() if end_dt else "",
                "location":  str(e.location() or ""),
                "calendar":  str(e.calendar().title() if e.calendar() else ""),
                # calendarItemExternalIdentifier is the iCal UID — the same
                # string the JXA reader reports and the only one the JXA write
                # path can look an event up by. calendarItemIdentifier is an
                # EventKit-store-local handle in a different namespace, so
                # returning it would silently break update_calendar_event the
                # moment full Calendar access is granted and reads switch to
                # EventKit. Kept as the fallback for events lacking an
                # external id.
                "uid":       str(e.calendarItemExternalIdentifier()
                                 or e.calendarItemIdentifier() or ""),
            })
        return out
    except Exception as e:
        # Don't go sticky here — a transient store error shouldn't permanently
        # demote the process to the slow path.
        logger.warning(f"EventKit read failed ({e}); falling back to JXA.")
        return None


def _ns_to_datetime(ns_date) -> datetime | None:
    """NSDate → naive local datetime, matching what the JXA path produces."""
    if ns_date is None:
        return None
    try:
        return datetime.fromtimestamp(ns_date.timeIntervalSince1970())
    except Exception:
        return None


def ensure_calendar_access() -> bool:
    """Trigger the EventKit permission prompt and report whether it was granted.

    Called from the GUI process at launch (mac_app) so the dialog appears with
    something in the foreground to attach to. TCC grants are per binary and the
    menu bar and core share one, so priming here is what lets the core take the
    fast path. Safe to call repeatedly — the result is cached.
    """
    return _eventkit_store() is not None


def _whitelist(cfg: dict) -> list[str] | None:
    cal_list = (cfg.get("agent", {}) or {}).get("briefing_calendars") or []
    return list(cal_list) if cal_list else None


def _jxa_names_script() -> str:
    """Just the calendar names — cheap, and needed to split the real scan up."""
    return """
const Calendar = Application('Calendar');
JSON.stringify(Calendar.calendars.name().filter(n => n));
"""


def _jxa_script(only: str | None, start: datetime, end: datetime) -> str:
    """Scan one named calendar, or every non-excluded one when `only` is None.

    Scanning one at a time is not a stylistic choice. osascript enforces its own
    Apple Event timeout — 120 s, independent of and shorter than our subprocess
    timeout — and a single script covering several busy calendars blows through
    it and dies with "AppleEvent timed out (-1712)", losing the work already
    done. One calendar per invocation gives each its own 120 s budget, so a slow
    calendar can no longer poison the ones after it.
    """
    only_js     = json.dumps(only) if only is not None else "null"
    excluded_js = json.dumps(list(_DEFAULT_EXCLUDED))
    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(end.timestamp() * 1000)
    return f"""
const Calendar = Application('Calendar');
const startDate = new Date({start_ms});
const endDate   = new Date({end_ms});
const only      = {only_js};
const excluded  = new Set({excluded_js});
const out = [];
const cals = only === null
    ? Calendar.calendars()
    : Calendar.calendars.whose({{name: only}})();
for (let i = 0; i < cals.length; i++) {{
    const cal = cals[i];
    let name;
    try {{ name = cal.name(); }} catch (e) {{ continue; }}
    if (only === null && excluded.has(name)) continue;
    try {{
        const evts = cal.events.whose({{_and: [
            {{startDate: {{">=": startDate}}}},
            {{startDate: {{"<": endDate}}}}
        ]}})();
        for (let j = 0; j < evts.length; j++) {{
            const e = evts[j];
            try {{
                out.push({{
                    title:     (e.summary()  || "").toString(),
                    start_iso: e.startDate().toISOString(),
                    end_iso:   e.endDate().toISOString(),
                    location:  (e.location() || "").toString(),
                    calendar:  name,
                    uid:       (e.uid()      || "").toString()
                }});
            }} catch (err) {{}}
        }}
    }} catch (err) {{}}
}}
JSON.stringify(out);
"""


def _run_jxa(script: str, what: str = "read"):
    """Run one osascript invocation. Returns the parsed JSON, or None on any
    failure — distinct from an empty list, which means "ran, found nothing"."""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Apple Calendar %s timed out (>%ss). Grant Friday full Calendar "
            "access (System Settings → Privacy & Security → Calendars) so it "
            "can use the fast EventKit reader instead.", what, _TIMEOUT_S)
        return None

    if result.returncode != 0:
        logger.error(f"osascript failed ({what}): {result.stderr.strip()[:200]}")
        return None

    out = result.stdout.strip()
    if not out:
        return []

    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        logger.error(f"Apple Calendar JSON parse error ({what}): {e}")
        return None


def _jxa_events(whitelist: list[str] | None, start: datetime,
                end: datetime) -> list[dict]:
    """The fallback reader: one osascript invocation per calendar.

    A calendar that times out is logged and skipped rather than taking the
    whole briefing down with it — a partial day beats an empty one.
    """
    names = _run_jxa(_jxa_names_script(), "calendar list")
    if not names:
        # Couldn't even enumerate — fall back to the single combined scan so a
        # machine where the name query fails still has a chance of an answer.
        return _run_jxa(_jxa_script(None, start, end)) or []

    out: list[dict] = []
    # Duplicate names are normal — a local "Family" and a shared "Family" both
    # exist here. `calendars.whose({name: …})` already returns every calendar
    # with that name, so visiting the name twice would scan both twice and
    # report every one of their events twice.
    for name in dict.fromkeys(names):
        if not _keep(name, whitelist):
            continue
        got = _run_jxa(_jxa_script(name, start, end), f"calendar {name!r}")
        if got:
            out.extend(got)
    return out


def _keep(name: str, whitelist: list[str] | None) -> bool:
    """The whitelist/exclusion rule, shared by both backends.

    A whitelist wins outright — if the user named a calendar, they want it,
    even one of the defaults-excluded ones.
    """
    if whitelist is not None:
        return name in whitelist
    return name not in _DEFAULT_EXCLUDED


def events_in_window(cfg: dict, start_date: date, end_date: date) -> list[dict]:
    """All events with start in [start_date 00:00 local, end_date 00:00 local)."""
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt   = datetime.combine(end_date,   datetime.min.time())
    whitelist = _whitelist(cfg)

    events = _eventkit_events(start_dt, end_dt)
    if events is None:                       # EventKit unavailable → JXA
        events = _jxa_events(whitelist, start_dt, end_dt)
    else:
        # The JXA script filters calendar-side; EventKit queries the whole
        # store in one shot, so the same rule is applied here instead.
        events = [e for e in events if _keep(e.get("calendar", ""), whitelist)]

    events.sort(key=lambda e: e.get("start_iso", ""))
    return events


def events_for_day(cfg: dict, target_date: date) -> list[dict]:
    """All events starting on target_date in whitelisted calendars."""
    return events_in_window(cfg, target_date, target_date + timedelta(days=1))
