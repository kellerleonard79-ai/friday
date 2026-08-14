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

WHAT 4TH AND 5TH PERIOD ACTUALLY ARE — A CORRECTION.

This module first modeled them as two separate periods at two separate times,
each teaching a different course depending on the letter. That is wrong, and
it is wrong in a way that shows: it puts two classes on the card every day
when the day only has one of them, and it asks for four course assignments
where there are two.

They are ONE TIME SLOT with TWO IDENTITIES. On an A day that slot is 4th
period; on a B day the same clock hour is 5th. There is no day with both.

So an alternating entry is a slot, not a period, and its `alternates` carry
the period NUMBER as well as the course:

    {"start": "10:45", "end": "11:35",
     "alternates": [{"letter": "A", "n": 4, "canvas_course": "…"},
                    {"letter": "B", "n": 5, "canvas_course": "…"}]}

One time on one entry is the load-bearing part. Two rows with equal times
would drift the first time somebody edited one of them, and nothing would
catch it — `current_period` would just start reporting whichever sorted
first. A slot cannot disagree with itself about when it is.

This shape is a strict superset of what it replaced: an entry whose two
alternates share one `n` is exactly the old "period 4 teaches X on A days and
Y on B days", so `ensure()` can migrate without deciding anything it does not
know, and a school that really does work the old way stays expressible.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls, datetime, time as time_cls, timedelta

logger = logging.getLogger("friday.schedule")

# Six slots, not seven periods. The fourth is the alternating one: 4th period
# on an A day, 5th on a B day, at the same time either way.
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
        {"start": "10:45", "end": "11:35", "alternates": [
            {"letter": "A", "n": 4, "canvas_course": None},
            {"letter": "B", "n": 5, "canvas_course": None},
        ]},
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


def _is_legacy_alternating(period: dict) -> bool:
    """A period carrying the pre-slot `alternates: [course_id, course_id]`."""
    alts = period.get("alternates")
    return (isinstance(alts, list) and bool(alts)
            and not all(isinstance(a, dict) for a in alts))


def _migrate_alternating(periods: list) -> list:
    """Collapse the legacy two-period rotation into one slot. Idempotent.

    The old shape was two periods at two times, each with a course per
    letter. The new one is a single slot with a period number per letter, so
    a migration has to decide which identity goes with which letter and which
    of the four configured courses survive.

    IT DECIDES NOTHING IT DOES NOT KNOW. Positional order is the only signal
    in the old data, so it is the only one used: the first alternating period
    is the A identity and keeps its own A course, the second is the B
    identity and keeps its own B course. The slot takes the FIRST period's
    times, because two times where there is one slot means one of them was
    always wrong and the earlier is the one the card was already showing.

    A config with a single alternating period is not a rotation of this kind
    at all — it is one period teaching two courses — so it converts in place
    with both identities keeping its number, which is what the old code meant
    by it.
    """
    legacy = [i for i, p in enumerate(periods)
              if isinstance(p, dict) and _is_legacy_alternating(p)]
    if not legacy:
        return periods

    def alt_at(period: dict, idx: int):
        alts = period.get("alternates") or []
        return alts[idx] if idx < len(alts) else None

    if len(legacy) >= 2:
        a_i, b_i = legacy[0], legacy[1]
        a_p, b_p = periods[a_i], periods[b_i]
        slot = {
            "start": a_p.get("start") or "",
            "end": a_p.get("end") or "",
            "alternates": [
                {"letter": "A", "n": a_p.get("n"),
                 "canvas_course": alt_at(a_p, 0)},
                {"letter": "B", "n": b_p.get("n"),
                 "canvas_course": alt_at(b_p, 1)},
            ],
        }
        logger.info(f"schedule: merged periods {a_p.get('n')} and "
                    f"{b_p.get('n')} into one alternating slot at "
                    f"{slot['start']}–{slot['end']}.")
        periods = [slot if i == a_i else p
                   for i, p in enumerate(periods) if i != b_i]
        legacy = [i for i, p in enumerate(periods)
                  if isinstance(p, dict) and _is_legacy_alternating(p)]

    for i in legacy:
        p = periods[i]
        p["alternates"] = [
            {"letter": "A", "n": p.get("n"), "canvas_course": alt_at(p, 0)},
            {"letter": "B", "n": p.get("n"), "canvas_course": alt_at(p, 1)},
        ]
    return periods


def ensure(cfg: dict) -> dict:
    """Lazy migration, matching dashboard/server.py::_migrate_config.

    Adds the block if absent, migrates the legacy alternating shape, and
    backfills any period key a hand-edited config is missing. Never
    overwrites a value the user has set.
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
    else:
        sched["periods"] = _migrate_alternating(sched["periods"])
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


def _alternates_of(period: dict) -> list[dict] | None:
    """This slot's identities as [{letter, n, course_id}], or None if it has one.

    Tolerates the legacy bare-id list so a caller handed raw, unmigrated
    config still gets the right answer rather than a crash — `ensure()`
    normally reshapes it long before this runs, but this function is also the
    one a test or a script reaches for directly.
    """
    alts = period.get("alternates")
    if not isinstance(alts, list) or not alts:
        return None
    out = []
    for i, a in enumerate(alts):
        letter = ("A", "B")[i] if i < 2 else str(i)
        if isinstance(a, dict):
            out.append({
                "letter": str(a.get("letter") or letter).upper(),
                "n": a.get("n", period.get("n")),
                "course_id": a.get("canvas_course"),
            })
        else:
            out.append({"letter": letter, "n": period.get("n"),
                        "course_id": a})
    return out


def _label(n) -> str:
    return "" if n is None else str(n)


def course_for_period(period: dict, letter: str | None) -> dict:
    """What this slot is, given the letter.

    A slot is one time and — when it alternates — two identities, so the
    answer is a period NUMBER as well as a course. Returns a shape the card
    renders directly rather than a bare id, because the three states are
    genuinely different and collapsing them loses the one that matters:

        resolved    one identity; `n` and `course_id` are it
        unresolved  an alternating slot on an unknown letter — BOTH identities
                    come back in `alternates`, labeled, `n` is None and
                    `label` is "4/5"
        unassigned  no course configured; `course_id` is empty and `n` is not

    An unknown letter and a letter with no identity in this slot resolve the
    same way — both mean "nothing here is known to be today", and a slot that
    guessed would be wrong on the one day of the rotation it matters.
    """
    alternates = _alternates_of(period)
    if alternates is None:
        n = period.get("n")
        return {"n": n, "label": _label(n),
                "course_id": period.get("canvas_course"),
                "alternating": False, "resolved": True, "alternates": []}

    match = None
    if letter:
        match = next((a for a in alternates
                      if a["letter"] == letter.upper()), None)
    if match is None:
        return {"n": None,
                "label": "/".join(_label(a["n"]) for a in alternates),
                "course_id": None, "alternating": True, "resolved": False,
                "alternates": alternates}
    return {"n": match["n"], "label": _label(match["n"]),
            "course_id": match["course_id"], "alternating": True,
            "resolved": True, "alternates": alternates}


def periods_with_courses(schedule_cfg: dict, letter: str | None) -> list[dict]:
    """Every slot, in order, with its identity resolved against the letter."""
    out = []
    for p in (schedule_cfg or {}).get("periods") or []:
        entry = {
            "start": p.get("start") or "",
            "end": p.get("end") or "",
        }
        entry.update(course_for_period(p, letter))
        out.append(entry)
    return out


#  A GAP IS NOT ALWAYS A PASSING PERIOD. The default placeholder schedule
#  alone has one: the alternating 4th/5th slot ends at 11:35 and 6th period
#  does not start until 13:15 — that 100-minute gap is lunch, not a walk
#  between classrooms, and a first cut of this function held the machine
#  awake for the whole of it. Nothing in the config marks a gap as "lunch"
#  explicitly (lunch is not its own period entry), so the only signal
#  available is duration: a real passing period is a few minutes, lunch is
#  not. `max_gap_minutes` draws that line, generously, rather than trying to
#  special-case a period count or a specific gap position.
DEFAULT_MAX_PASSING_MINUTES = 15


def passing_period_blocks(schedule_cfg: dict, day: date_cls,
                          lead_minutes: int = 2,
                          max_gap_minutes: int = DEFAULT_MAX_PASSING_MINUTES) -> list[dict]:
    """The gaps between bells, as wake windows — one per passing period.

    A block spans one period's end to the next period's start, which is a
    property of the TIME SLOTS, not of which letter day it is: an alternating
    slot is one time with two identities (see the module docstring), so the
    gap on either side of it is the same gap on an A day and a B day. This is
    why the function takes no letter.

    `wake_at` is `lead_minutes` before the block starts, never the block start
    itself — waking a sleeping Mac at the exact moment the bell rings is too
    late for it to be reachable BY the bell. Two periods back to back (no gap)
    produce no block; a gap longer than `max_gap_minutes` is lunch or a free
    period, not a passing period, and is excluded rather than turned into an
    hour-long hold; a schedule with fewer than two timed periods produces
    none at all.

    PURE, like the rest of this module: `day` is passed in rather than read
    off the clock, and this returns nothing on a non-school day rather than
    the caller having to remember to check `is_school_day` itself.
    """
    if not is_school_day(day):
        return []
    parsed = []
    for p in (schedule_cfg or {}).get("periods") or []:
        s, e = _parse_hhmm(p.get("start")), _parse_hhmm(p.get("end"))
        if s and e and s < e:
            parsed.append((s, e))
    parsed.sort(key=lambda x: x[0])

    blocks = []
    for i in range(len(parsed) - 1):
        gap_start, gap_end = parsed[i][1], parsed[i + 1][0]
        if gap_start >= gap_end:
            continue   # back-to-back bells — no passing period to wake for
        gap_minutes = (datetime.combine(day, gap_end)
                       - datetime.combine(day, gap_start)).total_seconds() / 60
        if gap_minutes > max_gap_minutes:
            continue   # lunch, a free period, etc. — not a passing period
        wake_dt = datetime.combine(day, gap_start) - timedelta(minutes=lead_minutes)
        blocks.append({
            "start": gap_start.strftime("%H:%M"),
            "end": gap_end.strftime("%H:%M"),
            "wake_at": wake_dt.time().strftime("%H:%M"),
            "label": f"Passing {i + 1}→{i + 2}",
        })
    return blocks


def current_period(schedule_cfg: dict, now: datetime,
                   letter: str | None = None) -> dict:
    """Where the clock is in the day.

    ALSO IMPLEMENTED CLIENT-SIDE (dashboard/static/app.js). That is deliberate
    and it is the one duplication in this feature: the browser has to be able
    to flip the card without asking, or the card stops being right the moment
    the daemon is unreachable — which on school Wi-Fi is most of the day. This
    copy exists so the same answer is available to anything server-side that
    needs it without a round trip through a browser.

    `period` and `next` are RESOLVED slots — the same shape
    `periods_with_courses` returns — so a caller asking "what period is it"
    on a B day is told 5, not handed a slot to work it out from. The letter
    is passed in for the same reason nothing else here reads a clock.

    Returns state one of: before | in_period | passing | after | no_periods.
    """
    parsed = []
    for entry in periods_with_courses(schedule_cfg, letter):
        start, end = _parse_hhmm(entry.get("start")), _parse_hhmm(entry.get("end"))
        if start and end:
            parsed.append((start, end, entry))
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
