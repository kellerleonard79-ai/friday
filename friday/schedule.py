"""
schedule.py
The bell schedule, and which letter day it is.

PURE FUNCTIONS OVER A CONFIG BLOCK. Nothing here reads the clock on its own,
queries anything, or writes anything — `now` and `today` are passed in, the
same rule policy/ follows and for the same reason: a rotation bug that only
reproduces at 07:58 on the third Tuesday is not one you can test by waiting.

WHY SCHOOL-DAY COUNTING AND NOT DATE PARITY — AND AN HONEST CORRECTION.

This was first written down claiming date parity "breaks the first weekend it
meets". THAT IS FALSE, and the check is in tests/test_schedule.py: over 400
days there are ZERO divergences between counting school days and calendar
parity for a two-letter pattern. A weekend is two days, an even skip, so
parity survives it untouched. The intuition was wrong and the test that was
supposed to prove it was asserting the right answer for the wrong reason.

What counting school days actually buys, measured over the same 400 days:

    pattern length 2  →   0 divergences from parity
    pattern length 3  → 190
    pattern length 4  → 141

So it is the model that stays correct if `pattern` is ever something other
than [A, B] — which is a config key, so it can be. For A/B specifically it
agrees with parity everywhere, and is kept because it is the model that
matches how a rotation is actually described rather than a coincidence about
even numbers that nobody would notice breaking.

NEITHER APPROACH SURVIVES A HOLIDAY. A one-day closure shifts the true count
by one and both methods miss it identically — verified in the tests against
an inserted Labor Day. That is not an argument for a holiday calendar (see
below); it is the reason MANUAL OVERRIDE is the load-bearing part of this
module and not a convenience.

There is no holiday calendar and there must not be one, deliberately: a wrong
holiday list is worse than none (it desynchronises silently and nobody knows
which side is wrong), and maintaining a real one is a yearly chore for a
system whose whole point is not being a chore. Every weekday counts as a
school day, and MANUAL OVERRIDE is what absorbs the drift — one tap, good for
today only.

The override expiring at midnight is the load-bearing half. Schools break
their own rotation constantly for assemblies, late starts and exam weeks; a
system that cannot be told it is wrong is one that gets ignored, and an
override that outlives the day it was set for is a system that is wrong
tomorrow in a way nobody remembers turning on.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls, datetime, time as time_cls, timedelta

logger = logging.getLogger("friday.schedule")

# Seven periods. 4th and 5th alternate A/B; the rest run daily.
#
# SHIPPED AS PLACEHOLDERS. The real bell times and the real first-A-day are
# Keller's to fill in, and the card is written to work before they are: an
# unassigned period is a valid state, and a null start_date means "show both
# alternates, labeled" rather than "guess".
DEFAULT_SCHEDULE: dict = {
    "bedtime": "23:00",
    "periods": [
        {"n": 1, "start": "08:00", "end": "08:50", "canvas_course": None},
        {"n": 2, "start": "08:55", "end": "09:45", "canvas_course": None},
        {"n": 3, "start": "09:50", "end": "10:40", "canvas_course": None},
        {"n": 4, "start": "10:45", "end": "11:35", "alternates": [None, None]},
        {"n": 5, "start": "12:20", "end": "13:10", "alternates": [None, None]},
        {"n": 6, "start": "13:15", "end": "14:05", "canvas_course": None},
        {"n": 7, "start": "14:10", "end": "15:00", "canvas_course": None},
    ],
    "ab_cycle": {
        "start_date": None,      # first A day, ISO YYYY-MM-DD. Unknown for now.
        "pattern": ["A", "B"],
        "manual_override": None,        # "A" | "B", forces today
        "manual_override_date": None,   # ISO day the override was set FOR
    },
}


def ensure(cfg: dict) -> dict:
    """Lazy migration, matching dashboard/server.py::_migrate_config.

    Adds the block if absent and backfills any period key a hand-edited config
    is missing. Never overwrites a value the user has set.
    """
    sched = cfg.get("schedule")
    if not isinstance(sched, dict):
        import copy
        cfg["schedule"] = copy.deepcopy(DEFAULT_SCHEDULE)
        return cfg

    sched.setdefault("bedtime", DEFAULT_SCHEDULE["bedtime"])
    if not isinstance(sched.get("periods"), list) or not sched["periods"]:
        import copy
        sched["periods"] = copy.deepcopy(DEFAULT_SCHEDULE["periods"])
    cycle = sched.setdefault("ab_cycle", {})
    if not isinstance(cycle, dict):
        cycle = sched["ab_cycle"] = {}
    cycle.setdefault("start_date", None)
    cycle.setdefault("pattern", ["A", "B"])
    cycle.setdefault("manual_override", None)
    cycle.setdefault("manual_override_date", None)
    return cfg


def _parse_hhmm(value: str | None) -> time_cls | None:
    if not value:
        return None
    try:
        hh, mm = (int(x) for x in str(value).split(":", 1))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return time_cls(hh, mm)
    except (ValueError, AttributeError):
        return None


def is_school_day(day: date_cls) -> bool:
    """Every weekday. See the module docstring on why there is no holiday list."""
    return day.weekday() < 5


def school_days_since(start: date_cls, today: date_cls) -> int:
    """School days elapsed from `start` to `today`, with `start` itself as 0.

    Counts the weekdays in the half-open range [start, today), so the first A
    day returns 0 and the next school day returns 1 REGARDLESS of how many
    calendar days sit between them. Friday → Monday is one step, not three.
    """
    if today <= start:
        return 0
    # Whole weeks contribute five each; the remainder is walked. Bounded work
    # for a start date years back, which a stale config will eventually be.
    whole_weeks, remainder = divmod((today - start).days, 7)
    count = whole_weeks * 5
    cursor = start + timedelta(days=whole_weeks * 7)
    for _ in range(remainder):
        if is_school_day(cursor):
            count += 1
        cursor += timedelta(days=1)
    return count


def letter_for(schedule_cfg: dict, today: date_cls) -> str | None:
    """Today's rotation letter, or None when it genuinely is not known.

    None is a real answer and the card renders it as "show both, labeled" —
    never as a guess. It comes back for three distinct reasons, all of which
    mean the same thing to the caller: no start date configured, a start date
    that will not parse, or a day the school is not open.

    A manual override wins outright, and only for the day it was set for.
    """
    cycle = (schedule_cfg or {}).get("ab_cycle") or {}

    override = cycle.get("manual_override")
    if override:
        on = cycle.get("manual_override_date")
        # An override with no date attached is honoured for today only. It can
        # only arrive that way from a hand-edited config, and the alternative
        # — honouring it forever — is the failure this expiry exists to stop.
        if not on or str(on) == today.isoformat():
            letter = str(override).strip().upper()
            pattern = [str(p).upper() for p in (cycle.get("pattern") or ["A", "B"])]
            if letter in pattern:
                return letter
            logger.warning(f"schedule: manual_override {override!r} is not in "
                           f"the pattern {pattern}; ignoring it.")

    if not is_school_day(today):
        return None

    raw_start = cycle.get("start_date")
    if not raw_start:
        return None
    try:
        start = date_cls.fromisoformat(str(raw_start))
    except ValueError:
        logger.warning(f"schedule: ab_cycle.start_date {raw_start!r} is not an "
                       f"ISO date; the rotation is unresolved.")
        return None

    pattern = [str(p).upper() for p in (cycle.get("pattern") or ["A", "B"])]
    if not pattern:
        return None
    if today < start:
        return None
    return pattern[school_days_since(start, today) % len(pattern)]


def course_for_period(period: dict, letter: str | None) -> dict:
    """Which course a period is teaching, given the letter.

    Returns a shape the card renders directly rather than a bare id, because
    the three states are genuinely different and collapsing them loses the one
    that matters:

        resolved    one course; `course` is it
        unresolved  an alternating period on an unknown letter — BOTH courses
                    come back in `alternates`, labeled, and `course` is None
        unassigned  no course configured; both fields empty
    """
    alternates = period.get("alternates")
    if isinstance(alternates, list) and len(alternates) >= 2:
        labels = ["A", "B"]
        pairs = [{"letter": labels[i] if i < len(labels) else str(i),
                  "course_id": alternates[i]}
                 for i in range(2)]
        if letter is None:
            return {"course_id": None, "alternating": True, "resolved": False,
                    "alternates": pairs}
        idx = 0 if letter.upper() == "A" else 1
        return {"course_id": alternates[idx], "alternating": True,
                "resolved": True, "alternates": pairs}
    return {"course_id": period.get("canvas_course"), "alternating": False,
            "resolved": True, "alternates": []}


def periods_with_courses(schedule_cfg: dict, letter: str | None) -> list[dict]:
    """Every period, in order, with its course resolved against the letter."""
    out = []
    for p in (schedule_cfg or {}).get("periods") or []:
        entry = {
            "n": p.get("n"),
            "start": p.get("start") or "",
            "end": p.get("end") or "",
        }
        entry.update(course_for_period(p, letter))
        out.append(entry)
    return out


def current_period(schedule_cfg: dict, now: datetime) -> dict:
    """Where the clock is in the day.

    ALSO IMPLEMENTED CLIENT-SIDE (dashboard/static/app.js). That is deliberate
    and it is the one duplication in this feature: the browser has to be able
    to flip the card without asking, or the card stops being right the moment
    the daemon is unreachable — which on school Wi-Fi is most of the day. This
    copy exists so the same answer is available to anything server-side that
    needs it without a round trip through a browser.

    Returns state one of: before | in_period | passing | after | no_periods.
    """
    periods = (schedule_cfg or {}).get("periods") or []
    parsed = []
    for p in periods:
        start, end = _parse_hhmm(p.get("start")), _parse_hhmm(p.get("end"))
        if start and end:
            parsed.append((start, end, p))
    if not parsed:
        return {"state": "no_periods", "period": None, "next": None}

    parsed.sort(key=lambda x: x[0])
    t = now.time()

    if t < parsed[0][0]:
        return {"state": "before", "period": None, "next": parsed[0][2]}
    for i, (start, end, p) in enumerate(parsed):
        if start <= t < end:
            nxt = parsed[i + 1][2] if i + 1 < len(parsed) else None
            return {"state": "in_period", "period": p, "next": nxt}
        if i + 1 < len(parsed) and end <= t < parsed[i + 1][0]:
            return {"state": "passing", "period": None, "next": parsed[i + 1][2]}
    return {"state": "after", "period": None, "next": None}
