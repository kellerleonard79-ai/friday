"""
tools/ledger.py
The fact ledger: what this turn has actually read.

A PRECONDITION RECORDS WHAT WAS READ, NOT THAT A READ HAPPENED. That sentence
is the whole design. "The model called get_schedule at some point this turn" is
not a fact about anything — a calendar read covering next week does not license
a write to today, and a boolean "has_read_calendar" flag cannot tell the two
apart. So the ledger stores coverage, and a precondition asks whether the
specific thing it needs is inside it.

No tool in step 3 has a precondition; all of them are reads. The mechanism is
built now, with tests, because step 4's first gated write depends on it and the
moment you are writing to someone's real calendar is the wrong moment to be
debugging whether the check works.

The ledger is per-turn and lives in the turn runner, not in a module global:
two turns in flight on different executor threads must not see each other's
facts. It is deliberately not the read cache in tools/calendar_read.py — that
holds payload and may evict; this holds coverage and may not. If a precondition
could pass because something was still cached rather than because it was
actually read, the check would be measuring the wrong thing.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CalendarRead:
    """A half-open [start, end) day range this turn has actually read.

    Half-open because that is what the backend window is, and converting
    between inclusive and exclusive in two places is how an off-by-one day
    gets into a calendar write.
    """
    start: date
    end: date

    def covers(self, start: date, end: date) -> bool:
        return self.start <= start and self.end >= end


@dataclass(slots=True)
class Ledger:
    """One turn's facts. Append-only within the turn."""
    calendar_reads: list[CalendarRead] = field(default_factory=list)

    def record_calendar_read(self, start: date, end: date) -> None:
        self.calendar_reads.append(CalendarRead(start=start, end=end))

    def covers_calendar(self, start: date, end: date) -> bool:
        """Whether [start, end) has been read this turn.

        Any single recorded read must cover the whole range. Two adjacent
        reads are NOT stitched into one span on purpose: stitching is where a
        gap between them becomes invisible, and the cost of being strict is one
        extra tool call while the cost of being wrong is a double-booked
        calendar.
        """
        return any(r.covers(start, end) for r in self.calendar_reads)

    def summary(self) -> dict:
        """For the tool_calls log — what was known when a call was made."""
        return {
            "calendar_reads": [
                f"{r.start.isoformat()}/{r.end.isoformat()}" for r in self.calendar_reads
            ]
        }


class Precondition(Protocol):
    """A fact a tool needs before it may run.

    check() returns None when satisfied, or a sentence explaining what is
    missing — written TO THE MODEL, since the model is what will go and satisfy
    it. Arguments are the call's own, so a precondition can be about the
    specific range being written to rather than about calendars in general.
    """

    def check(self, ledger: Ledger, arguments: dict) -> str | None: ...


@dataclass(frozen=True, slots=True)
class CalendarReadFor:
    """Requires that the day a call targets has already been read this turn.

    Step 4's use: a write to 2026-08-12 requires that 2026-08-12 was read, so
    the model has actually looked before asserting. Unused in step 3 — every
    tool here is the read that would satisfy it.

    `date_field` names which of the call's own arguments carries the date,
    because "the day this call is about" is per-tool and hardcoding a parameter
    name here would make the precondition silently wrong on the next tool.
    """
    date_field: str = "date"

    def check(self, ledger: Ledger, arguments: dict) -> str | None:
        raw = arguments.get(self.date_field)
        if raw is None:
            return (
                f"{self.date_field} is required before this can be checked "
                f"against what has been read."
            )
        try:
            day = date.fromisoformat(str(raw)[:10])
        except Exception:
            return f"{self.date_field}={raw!r} is not an ISO date."

        if ledger.covers_calendar(day, day + timedelta(days=1)):
            return None
        return (
            f"The calendar for {day.isoformat()} has not been read in this "
            f"conversation turn. Call get_schedule for that day first."
        )


# ── Per-turn installation ────────────────────────────────────────────────────

# Thread-local because tools run in the executor, which is a pool: two turns
# can be in flight on different threads and must not see each other's facts.
_local = threading.local()


def begin_turn() -> Ledger:
    """Install a fresh ledger for this thread's turn and return it."""
    led = Ledger()
    _local.ledger = led
    return led


def current() -> Ledger | None:
    """This turn's ledger, or None outside a turn.

    None rather than an empty Ledger on purpose. A tool that records into a
    ledger nobody will read is harmless; a precondition that PASSES because it
    consulted a fresh empty ledger it created itself would be the exact failure
    this module exists to prevent, so the absence has to be visible.
    """
    return getattr(_local, "ledger", None)


def end_turn() -> None:
    _local.ledger = None
