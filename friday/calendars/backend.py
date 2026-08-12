"""
calendars/backend.py
Calendar backend dispatch — the one place that decides whether Friday's
event store is Apple Calendar (macOS, JXA) or Google Calendar (Windows, API).

Selection: config `calendar.backend` ("apple" | "google"), defaulting to
google on Windows and apple everywhere else.

friday.py calls init(config) once at startup so the write path (which is
reached from actions/calendar.py without a cfg in hand) knows the config.
Reads take cfg explicitly, matching the old apple_calendar signatures.
"""

import dataclasses
import logging
import sys
import time

from calendars.writes import WriteOutcome

logger = logging.getLogger("friday.calbackend")

_CONFIG: dict = {}


def init(config: dict) -> None:
    global _CONFIG
    _CONFIG = config or {}
    logger.info(f"Calendar backend: {backend_name(_CONFIG)}")


def backend_name(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else _CONFIG
    explicit = ((cfg.get("calendar") or {}).get("backend") or "").strip().lower()
    if explicit in ("apple", "google"):
        return explicit
    return "google" if sys.platform == "win32" else "apple"


def _mod(cfg: dict | None = None):
    if backend_name(cfg) == "google":
        from calendars import google_cal
        return google_cal
    from calendars import apple
    return apple


# ── Reads ─────────────────────────────────────────────────────────────────────

def events_in_window(cfg: dict, start_date, end_date) -> list[dict]:
    return _mod(cfg).events_in_window(cfg, start_date, end_date)


def events_for_day(cfg: dict, target_date) -> list[dict]:
    return _mod(cfg).events_for_day(cfg, target_date)


# ── Writes ────────────────────────────────────────────────────────────────────

def write_event(calendar_name: str, title: str, start, end,
                location: str = "", description: str = "",
                all_day: bool = False, verify: bool = True) -> WriteOutcome:
    """Create an event. Returns a WriteOutcome — see calendars/writes.py for
    why this is not `str | None`.

    THE READ-BACK IS WHERE INVARIANT 4 IS EARNED. A write path that returns an
    identifier it read off the object it just constructed has confirmed
    nothing; it has agreed with itself. Invariant 4 says a write is confirmed
    to the user only after the service confirms it back, so on a successful
    write we go and ask the service whether an event with that identifier is
    actually on that day, and report the answer in `verified`.

    It is done here rather than in each backend so there is one implementation
    of "confirmed" and the two backends cannot drift on what it means.

    A read-back that fails does NOT downgrade the outcome to unknown. The write
    reported an identifier, which is real evidence; a failed read is evidence
    about the reader. Callers that need certainty check `verified`, and the
    honest report is "written but unconfirmed" rather than either lie.

    `verify=False` exists for bulk paths — gcal_sync mirrors dozens of events
    per poll and a read-back per event would be the dominant cost. It is not a
    convenience for the write path a user is waiting on.
    """
    outcome = _mod().write_event(_CONFIG, calendar_name, title, start, end,
                                 location=location, description=description,
                                 all_day=all_day)
    if not verify or not outcome.ok:
        return outcome
    return dataclasses.replace(
        outcome, verified=_readback_confirms(outcome.uid, start))


# The read-back races the write it is checking. On the Apple backend the two
# use different views of the same database — writes go out through JXA, reads
# come back through a cached EKEventStore that only refreshes when it processes
# a change notification — so a read twenty milliseconds after a write reliably
# missed it and reported a real write as unconfirmed.
#
# refresh() plus a short bounded retry closes it. Bounded tightly because the
# user is waiting on a confirmation: three tries is enough for the notification
# to land, and a fourth would be waiting on something that is not coming.
_READBACK_TRIES = 3
_READBACK_PAUSE_S = 0.4


def _readback_confirms(uid: str, start) -> bool:
    """Whether the service reports an event under `uid` on `start`'s day.

    Wrapped: a read-back that raises must not fail a write that already
    succeeded. It reports False, which reads as "unconfirmed" — the honest
    state — rather than taking down the turn.
    """
    if not uid:
        return False
    day = start.date() if hasattr(start, "date") else start
    mod = _mod()
    for attempt in range(_READBACK_TRIES):
        try:
            if attempt:
                time.sleep(_READBACK_PAUSE_S)
            # Before every attempt including the first: the write happened
            # microseconds ago and the store is stale by construction.
            try:
                mod.refresh()
            except Exception as e:
                logger.debug(f"read-back refresh failed: {e}")
            for evt in mod.events_for_day(_CONFIG, day):
                if evt.get("uid") == uid:
                    return True
        except Exception as e:
            logger.warning(f"Write read-back failed (reporting unconfirmed): {e}")
            return False
    logger.warning(
        f"Write reported uid {uid} but {_READBACK_TRIES} read-backs of {day} "
        f"did not find it. Reporting the event as written but unconfirmed."
    )
    return False


def update_event(uid: str, calendar_name: str = "", **fields) -> dict | None:
    """Modify an existing event in place. `fields` accepts title/start/end/
    location/description/all_day; anything left out keeps its current value.
    `calendar_name` is a lookup hint — supply it when known, since the Apple
    backend otherwise scans every calendar. Returns the event's post-update
    state, or None on failure."""
    return _mod().update_event(_CONFIG, uid, calendar_name=calendar_name,
                               **fields)


def calendar_exists(name: str) -> bool:
    return _mod().calendar_exists(_CONFIG, name)
