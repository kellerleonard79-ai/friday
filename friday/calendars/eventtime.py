"""
calendars/eventtime.py
Reading times out of an event dict, once, for everyone.

The two backends do not agree on how they spell a time, and the disagreement is
silent — both produce a parseable ISO string, so code that gets it wrong keeps
answering, five hours off:

  * The Apple JXA reader emits UTC with a trailing Z ("2026-08-12T05:00:00.000Z").
  * The Apple EventKit reader emits a NAIVE LOCAL datetime for the same event.
  * The Google backend emits date-only strings for all-day events.

Which of the first two you get depends on whether the process holds a Calendar
TCC grant, so the same code path can be correct on one machine and wrong on
another with nothing in the logs to say so. That is what this module exists to
make impossible.

Neither backend returns an all-day flag either, so it has to be derived — and
the derivation is where the last bug lived. See is_all_day.
"""

from __future__ import annotations

from datetime import datetime

# How far from a whole number of days a span may be and still be all-day.
#
# THIS TOLERANCE IS THE POINT. Apple reports an all-day event as midnight to
# 23:59:59 — a span of 86399 seconds, not 86400. An exact `span % 86400 == 0`
# test therefore called every real all-day event "timed", which is how a
# school calendar full of all-day entries turns into a day with no free time
# in it. Anything within a minute of a whole day is a whole day.
_ALL_DAY_SLACK_S = 60


def to_local(iso: str) -> datetime | None:
    """An event's ISO timestamp as an aware local datetime, or None.

    .astimezone() is what reconciles the two Apple readers: it converts an
    aware UTC value and localizes a naive one, so both spellings of the same
    moment land on the same wall clock.
    """
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def is_all_day(evt: dict) -> bool:
    """Whether an event occupies whole days rather than a slot within one.

    Three shapes, because three backends:
      * Google all-day — a date-only string, no "T".
      * Apple all-day  — local midnight, spanning ~n whole days (see the slack).
      * everything else is timed.
    """
    start = evt.get("start_iso", "")
    if start and "T" not in start and len(start) == 10:
        return True

    sd = to_local(start)
    ed = to_local(evt.get("end_iso", ""))
    if not (sd and ed):
        return False
    if (sd.hour, sd.minute) != (0, 0):
        return False

    span = (ed - sd).total_seconds()
    if span < 86400 - _ALL_DAY_SLACK_S:
        return False
    days = round(span / 86400)
    return days >= 1 and abs(span - days * 86400) <= _ALL_DAY_SLACK_S
