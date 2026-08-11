"""
tools/calendar_read.py
The two read-only calendar tools, and the per-turn cache that makes them
affordable.

READ-ONLY. Neither of these writes; writes and their permission gate are step 4.

WHY THERE IS A CACHE. It was built when every calendar read cost 26-35 seconds,
because the daemon lacked a Calendars TCC grant and fell back to the JXA
reader — where the cost is per-calendar-scanned, not per-day-requested, so a
one-day window cost the same as a week. Against CHAT's 120s deadline that made
"what's tomorrow?" followed by "when am I free?" a routine timeout.

That grant now exists and the daemon reads through EventKit, which answers from
the local store in milliseconds. The cache is therefore an optimization rather
than the thing that makes the loop fit its budget. It is kept because a read
that costs nothing is still worth not doing twice, and because the JXA fallback
is one revoked permission away — see connectors/apple_calendar.py, where the
status check runs on every call precisely so a grant can come and go without a
restart.

The cache is per-turn and holds payload; the fact ledger holds records. They
are deliberately not the same object, and this module can only reach one of
them: if the ledger's answer to "what has been read" depended on cache
eviction, a precondition could pass because something was still cached rather
than because it was actually read. Records are DECLARED in each tool's return
value and written to the ledger by tools/executor.py — see tools/types.py.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Annotated

from calendars import backend as calendar_backend
from calendars.eventtime import is_all_day, to_local
from tools import scratch
from tools.registry import tool
from tools.types import CalendarRead, ToolError, ToolOutcome, ToolResult

logger = logging.getLogger("friday.tools.calendar")

_config: dict = {}


def configure(config: dict) -> None:
    """Install the config the calendar backend needs. Called once at startup."""
    global _config
    _config = config or {}


# ── The per-turn read cache ──────────────────────────────────────────────────

# The key this module keeps its cache under in the turn's scratch space.
_SCRATCH = "calendar_read"


def _windows() -> dict | None:
    """This turn's cached reads, keyed by the window fetched, or None.

    Lives in tools/scratch.py rather than a module thread-local because tools
    run in a worker pool: a thread-local installed by the turn is invisible
    from inside the tool, which is precisely why this cache never once hit
    before that was fixed.
    """
    return scratch.space(_SCRATCH)


def _read_window(start: date, end: date) -> list[dict]:
    """Events in [start, end), from cache when a previous read covered it.

    A wider cached window satisfies a narrower request by filtering, which is
    what makes the second tool call in a turn free: the cost is per scan, so a
    read that already happened covers everything inside it.
    """
    cache = _windows()

    if cache is not None:
        for (c_start, c_end), events in cache.items():
            if c_start <= start and c_end >= end:
                return [e for e in events if _in_window(e, start, end)]

    events = calendar_backend.events_in_window(_config, start, end)
    if cache is not None:
        cache[(start, end)] = events
    return events


def _in_window(evt: dict, start: date, end: date) -> bool:
    sd = to_local(evt.get("start_iso", ""))
    return bool(sd and start <= sd.date() < end)


# ── Shaping ──────────────────────────────────────────────────────────────────

def _shape(evt: dict) -> dict:
    """One event as structured data. Never prose — see tools/types.py."""
    sd = to_local(evt.get("start_iso", ""))
    ed = to_local(evt.get("end_iso", ""))
    all_day = is_all_day(evt)
    out = {
        "title": evt.get("title", ""),
        "calendar": evt.get("calendar", ""),
        "all_day": all_day,
        "date": sd.date().isoformat() if sd else "",
    }
    if not all_day:
        out["start"] = sd.isoformat(timespec="minutes") if sd else ""
        out["end"] = ed.isoformat(timespec="minutes") if ed else ""
    if evt.get("location"):
        out["location"] = evt["location"]
    return out


def _parse_date(value: str, field: str) -> date | ToolError:
    try:
        return date.fromisoformat(value.strip()[:10])
    except Exception:
        return ToolError(
            kind="invalid_argument",
            message=f"{field}={value!r} is not an ISO date. Use YYYY-MM-DD.",
            field=field,
        )


def _parse_time(value: str, field: str) -> time | ToolError:
    try:
        return time.fromisoformat(value.strip())
    except Exception:
        return ToolError(
            kind="invalid_argument",
            message=f"{field}={value!r} is not a 24-hour time. Use HH:MM.",
            field=field,
        )


# ── The tools ────────────────────────────────────────────────────────────────

@tool(
    name="get_schedule",
    description="Returns the events on the user's calendar between two dates, inclusive.",
    scope=("read",),
    effect="read",
    timeout_s=60.0,
)
def get_schedule(
    date_from: Annotated[str, "First day to include, ISO YYYY-MM-DD."],
    date_to: Annotated[str, "Last day to include, ISO YYYY-MM-DD. Same as date_from for a single day."],
) -> ToolOutcome:
    """Call when the user asks what is scheduled, or before answering any
    question that depends on what is already on the calendar."""
    start = _parse_date(date_from, "date_from")
    if isinstance(start, ToolError):
        return start
    end = _parse_date(date_to, "date_to")
    if isinstance(end, ToolError):
        return end
    if end < start:
        return ToolError(
            kind="invalid_argument",
            message=f"date_to ({date_to}) is before date_from ({date_from}).",
            field="date_to",
        )

    try:
        # date_to is inclusive to the model; the backend window is half-open.
        window_end = end + timedelta(days=1)
        events = _read_window(start, window_end)
    except Exception as e:
        logger.warning(f"get_schedule read failed: {e}")
        return ToolError(
            kind="unavailable",
            message=f"The calendar could not be read: {e}",
        )

    return ToolResult(
        data={
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "events": [_shape(e) for e in events],
        },
        # Declared, not recorded. The executor puts this in the ledger; this
        # module cannot reach the ledger and must not try.
        records=(CalendarRead(start=start, end=window_end),),
    )


@tool(
    name="find_free_blocks",
    description="Returns the gaps on a given day that are free for at least a stated number of minutes.",
    scope=("read",),
    effect="read",
    timeout_s=60.0,
)
def find_free_blocks(
    date: Annotated[str, "The day to search, ISO YYYY-MM-DD."],
    min_minutes: Annotated[int, "Shortest gap worth returning, in minutes."] = 30,
    between_start: Annotated[str, "Earliest time of day to consider, 24-hour HH:MM."] = "08:00",
    between_end: Annotated[str, "Latest time of day to consider, 24-hour HH:MM."] = "22:00",
) -> ToolOutcome:
    """Call when the user asks when they are free, or whether something fits."""
    day = _parse_date(date, "date")
    if isinstance(day, ToolError):
        return day
    lo = _parse_time(between_start, "between_start")
    if isinstance(lo, ToolError):
        return lo
    hi = _parse_time(between_end, "between_end")
    if isinstance(hi, ToolError):
        return hi
    if hi <= lo:
        return ToolError(
            kind="invalid_argument",
            message=f"between_end ({between_end}) is not after between_start ({between_start}).",
            field="between_end",
        )
    if min_minutes < 1:
        return ToolError(
            kind="invalid_argument",
            message=f"min_minutes must be at least 1, got {min_minutes}.",
            field="min_minutes",
        )

    try:
        events = _read_window(day, day + timedelta(days=1))
    except Exception as e:
        logger.warning(f"find_free_blocks read failed: {e}")
        return ToolError(
            kind="unavailable",
            message=f"The calendar could not be read: {e}",
        )

    window_start = datetime.combine(day, lo).astimezone()
    window_end = datetime.combine(day, hi).astimezone()

    # ALL-DAY EVENTS DO NOT BLOCK TIME.
    #
    # An all-day event is a label on the day, not a claim on the hours in it.
    # Treating one as a 24-hour busy block reports zero free time on every
    # school day, which is worse than useless. They are returned separately so
    # the model knows they exist and can mention them.
    busy: list[tuple[datetime, datetime]] = []
    all_day: list[dict] = []
    for evt in events:
        if is_all_day(evt):
            all_day.append(_shape(evt))
            continue
        sd, ed = to_local(evt.get("start_iso", "")), to_local(evt.get("end_iso", ""))
        if not (sd and ed) or ed <= sd:
            continue
        # Clip to the window; an event running past either edge constrains
        # only the part of it that is inside.
        sd, ed = max(sd, window_start), min(ed, window_end)
        if sd < ed:
            busy.append((sd, ed))

    # THE DETERMINISM BOUNDARY. The model never receives a raw event list and
    # is never asked to find the holes in it. Merge overlaps, then walk the
    # gaps — arithmetic, in Python, with an answer that is the same every time.
    busy.sort()
    merged: list[list[datetime]] = []
    for sd, ed in busy:
        if merged and sd <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], ed)
        else:
            merged.append([sd, ed])

    gaps = []
    cursor = window_start
    for sd, ed in merged + [[window_end, window_end]]:
        if sd > cursor:
            minutes = int((sd - cursor).total_seconds() // 60)
            if minutes >= min_minutes:
                gaps.append({
                    "start": cursor.isoformat(timespec="minutes"),
                    "end": sd.isoformat(timespec="minutes"),
                    "minutes": minutes,
                })
        cursor = max(cursor, ed)

    return ToolResult(
        data={
            "date": day.isoformat(),
            "between": {"start": between_start, "end": between_end},
            "min_minutes": min_minutes,
            "free_blocks": gaps,
            # Named so the model can say "you're free, though it's Tiger Camp
            # day" rather than silently dropping something the user cares about.
            "all_day_events": all_day,
        },
        records=(CalendarRead(start=day, end=day + timedelta(days=1)),),
    )
