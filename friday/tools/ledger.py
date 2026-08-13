"""
tools/ledger.py
The fact ledger: what this turn has actually established.

A PRECONDITION RECORDS WHAT WAS READ, NOT THAT A READ HAPPENED. That sentence
is the whole design. "The model called get_schedule at some point this turn" is
not a fact about anything — a calendar read covering next week does not license
a write to today, and a boolean has_read_calendar flag cannot tell the two
apart. So the ledger stores coverage, and a precondition asks whether the
specific thing it needs is inside it.

THERE IS NO MODULE-LEVEL ACCESSOR HERE, AND THAT IS THE POINT.

The ledger used to live in a thread-local that any tool could reach and write
to. Two things were wrong with that. The first is integrity: a tool recording
its own coverage is a tool the precondition is trusting to tell the truth about
the very thing being checked. The second was a plain bug — tools execute in a
worker pool, the thread-local was installed on the turn thread, so from inside
a tool the ledger was invisible anyway and every precondition failed closed.
Fixing only the first would have left a ledger nobody could see.

So the Ledger is now an ordinary object. agent/turn.py creates one per turn and
hands it to tools/executor.py, which is the only writer: it takes the coverage
a tool RETURNED and records that. Tool code cannot reach this module — verify
with `grep -rn "ledger" friday/tools/calendar_read.py`, which finds nothing.

No thread-local, no global, no singleton. If a future caller needs the ledger,
it gets passed one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

from tools.types import CalendarRead, CalendarWrite, Record, WriteAttempt


@dataclass(slots=True)
class Ledger:
    """One turn's established facts. Append-only within the turn."""
    entries: list[Record] = field(default_factory=list)

    def record(self, records: tuple[Record, ...] | list[Record]) -> None:
        """Write what a call established. Called by the executor — from a read
        tool's declared records, or from what the executor synthesised out of a
        write's confirmation. Never by the tool itself."""
        self.entries.extend(records)

    def covers_calendar(self, start: date, end: date) -> bool:
        """Whether [start, end) has been read this turn.

        A single recorded read must cover the whole range. Two adjacent reads
        are NOT stitched into one span on purpose: stitching is where a gap
        between them becomes invisible, and being strict costs one extra tool
        call while being wrong costs a double-booked calendar.
        """
        return any(
            e.covers(start, end)
            for e in self.entries if isinstance(e, CalendarRead)
        )

    def wrote(self, fingerprint: str) -> bool:
        """Whether this turn already wrote, or may have written, this exact
        thing. An ATTEMPT counts: the question a caller asks here is "could a
        write now double-book", and "we do not know whether the first one
        landed" has to answer yes to that. The ten-minute recent_writes table
        (memory/writes.py) is the cross-turn version of the same question."""
        return any(
            isinstance(e, (CalendarWrite, WriteAttempt))
            and e.fingerprint == fingerprint
            for e in self.entries
        )

    def summary(self) -> dict:
        """For the tool_calls log — what was known when a call was made.

        This is what answers "what had Friday actually read when it wrote?",
        which is the first question anybody asks after a bad write. Persisted
        onto the write's tool_calls row; see memory/activity.py.
        """
        return {
            "calendar_reads": [
                f"{e.start.isoformat()}/{e.end.isoformat()}"
                for e in self.entries if isinstance(e, CalendarRead)
            ],
            "calendar_writes": [
                {"id": e.identifier, "day": e.day.isoformat(),
                 "fingerprint": e.fingerprint, "verified": e.verified}
                for e in self.entries if isinstance(e, CalendarWrite)
            ],
            "write_attempts": [
                {"day": e.day.isoformat(), "fingerprint": e.fingerprint,
                 "detail": e.detail}
                for e in self.entries if isinstance(e, WriteAttempt)
            ],
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

    A write to 2026-08-12 requires that 2026-08-12 was read, so the model has
    actually looked before asserting.

    STILL NO LIVE CONSUMER. add_calendar_event correctly carries none — the
    user knows what they are asking for, and forcing a read before every add
    costs a hop to establish something nobody needed. The tools that will use
    this are update and delete, and tools/preconditions.py holds the exact
    tuples they will be registered with, with tests, so the step that adds
    them is not also discovering what a precondition means.

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
