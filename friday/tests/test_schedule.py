"""
tests/test_schedule.py
The bell schedule and the A/B rotation. Plain asserts, no test framework.

    python3 tests/test_schedule.py     (from the friday/ package directory)

THE ROTATION IS THE REASON THIS FILE EXISTS, and the first version of this
docstring was wrong about why. It claimed the weekend cases below are ones
"parity gets wrong". They are not — see the divergence block at the bottom,
which proves parity and school-day counting agree everywhere for a two-letter
pattern, because a weekend is an even skip.

The weekend cases are still worth asserting: they pin the behavior that is
correct, whatever the reason it is correct. What actually justifies the
counter is pattern length, and what actually breaks both is a holiday. Both
are measured at the bottom rather than asserted from intuition.
"""

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import schedule                                                  # noqa: E402

_failures: list[str] = []


def check(label: str, ok: bool) -> None:
    if not ok:
        _failures.append(label)
    print(f"{'ok  ' if ok else 'FAIL'}  {label}")


def cycle(**kw) -> dict:
    c = {"start_date": None, "pattern": ["A", "B"],
         "manual_override": None, "manual_override_date": None}
    c.update(kw)
    return {"ab_cycle": c, "periods": schedule.DEFAULT_SCHEDULE["periods"]}


# ── school_days_since: the counter parity gets wrong ─────────────────────────

MON = date(2026, 8, 10)      # a Monday
TUE, WED, THU, FRI = (date(2026, 8, d) for d in (11, 12, 13, 14))
SAT, SUN = date(2026, 8, 15), date(2026, 8, 16)
NEXT_MON = date(2026, 8, 17)

check("start date itself is 0", schedule.school_days_since(MON, MON) == 0)
check("next day is 1", schedule.school_days_since(MON, TUE) == 1)
check("Mon→Fri is 4", schedule.school_days_since(MON, FRI) == 4)

# THE WEEKEND CASE. Friday is index 4; the next school day is Monday, which
# must be 5. Calendar-date parity makes it 7 and flips the letter back.
check("Fri→Mon is one step, not three",
      schedule.school_days_since(MON, NEXT_MON) == 5)
check("a full week later is exactly 5 school days",
      schedule.school_days_since(MON, NEXT_MON)
      - schedule.school_days_since(MON, MON) == 5)
check("four weeks later is 20 school days",
      schedule.school_days_since(MON, date(2026, 9, 7)) == 20)
check("a date before the start clamps to 0",
      schedule.school_days_since(MON, date(2026, 8, 3)) == 0)


# ── letter_for ───────────────────────────────────────────────────────────────

check("no start date means unresolved, not a guess",
      schedule.letter_for(cycle(), MON) is None)
check("unparseable start date is unresolved, not a crash",
      schedule.letter_for(cycle(start_date="not-a-date"), MON) is None)
check("a date before the first A day is unresolved",
      schedule.letter_for(cycle(start_date="2026-08-10"), date(2026, 8, 5)) is None)

live = cycle(start_date="2026-08-10")      # Monday the 10th is the first A day
check("the first A day is A", schedule.letter_for(live, MON) == "A")
check("the day after is B", schedule.letter_for(live, TUE) == "B")
check("and back to A", schedule.letter_for(live, WED) == "A")
check("Thursday is B", schedule.letter_for(live, THU) == "B")
check("Friday is A", schedule.letter_for(live, FRI) == "A")

# Friday A → Monday B. (Calendar parity happens to agree here; see the
# divergence block at the bottom. Asserted anyway — it is the right answer.)
check("Monday after a Friday-A is B",
      schedule.letter_for(live, NEXT_MON) == "B")

check("Saturday has no letter", schedule.letter_for(live, SAT) is None)
check("Sunday has no letter", schedule.letter_for(live, SUN) is None)

# A three-letter rotation is not what this school runs, but nothing in the
# code should assume two — the pattern is config.
check("a 3-letter pattern rotates on its own length",
      [schedule.letter_for(cycle(start_date="2026-08-10", pattern=["A", "B", "C"]), d)
       for d in (MON, TUE, WED, THU)] == ["A", "B", "C", "A"])


# ── manual override ──────────────────────────────────────────────────────────

ov = cycle(start_date="2026-08-10", manual_override="B",
           manual_override_date="2026-08-10")
check("an override for today wins over the count",
      schedule.letter_for(ov, MON) == "B")
# EXPIRY AT MIDNIGHT. Checked against Wednesday rather than Tuesday on
# purpose: Tuesday counts to B on its own, so an override of B that wrongly
# survived would be indistinguishable from the correct answer. Wednesday
# counts to A, so a leaked B is visible.
check("AND EXPIRES AT MIDNIGHT — a stale override does not decide tomorrow",
      schedule.letter_for(ov, WED) == "A")
check("an override on a weekend still applies (assembly Saturday)",
      schedule.letter_for(
          cycle(start_date="2026-08-10", manual_override="A",
                manual_override_date=SAT.isoformat()), SAT) == "A")
check("an override outside the pattern is ignored, not honoured",
      schedule.letter_for(
          cycle(start_date="2026-08-10", manual_override="Z",
                manual_override_date="2026-08-10"), MON) == "A")
check("an override with no date is honoured for today only",
      schedule.letter_for(
          cycle(start_date="2026-08-10", manual_override="B"), MON) == "B")


# ── course resolution ────────────────────────────────────────────────────────

daily = {"n": 1, "start": "08:00", "end": "08:50", "canvas_course": "208300"}
slot = {"start": "10:45", "end": "11:35", "alternates": [
    {"letter": "A", "n": 4, "canvas_course": "209288"},
    {"letter": "B", "n": 5, "canvas_course": "208473"}]}
empty = {"n": 2, "start": "08:55", "end": "09:45", "canvas_course": None}
legacy = {"n": 4, "start": "10:45", "end": "11:35",
          "alternates": ["209288", "208473"]}

check("a daily period resolves to its course",
      schedule.course_for_period(daily, "A")["course_id"] == "208300")
check("a daily period ignores the letter",
      schedule.course_for_period(daily, "B")["course_id"] == "208300")
check("an unassigned period is a valid state",
      schedule.course_for_period(empty, "A")["course_id"] is None)

# The point of the slot: ONE identity per day, and it is a period NUMBER as
# much as a course. An A day has a 4th period and no 5th.
a_day = schedule.course_for_period(slot, "A")
b_day = schedule.course_for_period(slot, "B")
check("the slot is 4th period on an A day",
      a_day["n"] == 4 and a_day["course_id"] == "209288")
check("the SAME slot is 5th period on a B day",
      b_day["n"] == 5 and b_day["course_id"] == "208473")
check("a resolved slot names one period, never both",
      a_day["label"] == "4" and b_day["label"] == "5")

unres = schedule.course_for_period(slot, None)
check("an unresolved slot picks NEITHER",
      unres["n"] is None and unres["course_id"] is None
      and unres["resolved"] is False)
check("...and returns both identities, labeled A and B",
      [x["letter"] for x in unres["alternates"]] == ["A", "B"]
      and [x["n"] for x in unres["alternates"]] == [4, 5]
      and [x["course_id"] for x in unres["alternates"]] == ["209288", "208473"])
check("...and labels itself with both numbers", unres["label"] == "4/5")

# A letter the slot has no identity for is the same answer as no letter at
# all. Guessing here would be wrong on the one day of the rotation it counts.
check("a letter outside the slot resolves to nothing, not to the first",
      schedule.course_for_period(slot, "C")["resolved"] is False)

# The legacy shape still reads correctly, so an unmigrated config is thin,
# never wrong.
check("a legacy alternates list still resolves by position",
      schedule.course_for_period(legacy, "A")["course_id"] == "209288"
      and schedule.course_for_period(legacy, "B")["course_id"] == "208473")
check("...keeping its single period number on both letters",
      schedule.course_for_period(legacy, "A")["n"] == 4
      and schedule.course_for_period(legacy, "B")["n"] == 4)


# ── current_period ────────────────────────────────────────────────────────────

sched = {"periods": schedule.DEFAULT_SCHEDULE["periods"]}


def at(h, m, letter=None):
    return schedule.current_period(sched, datetime(2026, 8, 12, h, m), letter)


check("before first bell", at(7, 30)["state"] == "before")
check("inside 1st period", at(8, 20)["state"] == "in_period"
      and at(8, 20)["period"]["n"] == 1)
check("the boundary belongs to the NEXT period, not the ending one",
      at(8, 50)["state"] == "passing")
check("passing time knows what is next", at(8, 52)["next"]["n"] == 2)
check("the long gap after the rotating slot is passing time",
      at(12, 30)["state"] == "passing")
check("inside 7th period", at(14, 30)["period"]["n"] == 7)
check("after the last bell", at(15, 30)["state"] == "after")
check("last period has no next", at(14, 30)["next"] is None)

# The rotating slot is one entry in the day and reports the letter's identity
# rather than the config's shape.
check("the rotating slot is 4th period on an A day",
      at(11, 0, "A")["period"]["n"] == 4)
check("the same clock minute is 5th period on a B day",
      at(11, 0, "B")["period"]["n"] == 5)
check("...and admits it does not know without a letter",
      at(11, 0)["period"]["resolved"] is False
      and at(11, 0)["period"]["label"] == "4/5")
check("the day has ONE slot at that hour, not two",
      len([p for p in schedule.periods_with_courses(sched, "A")
           if p["start"] == "10:45"]) == 1)

check("an empty schedule does not crash",
      schedule.current_period({"periods": []}, datetime(2026, 8, 12, 9, 0))
      ["state"] == "no_periods")
check("periods with unparseable times are skipped, not fatal",
      schedule.current_period(
          {"periods": [{"n": 1, "start": "oops", "end": "08:50"}]},
          datetime(2026, 8, 12, 9, 0))["state"] == "no_periods")


# ── ensure() ──────────────────────────────────────────────────────────────────

fresh: dict = {}
schedule.ensure(fresh)
check("ensure adds the block", isinstance(fresh.get("schedule"), dict))
check("ensure ships 6 SLOTS, not 7 periods",
      len(fresh["schedule"]["periods"]) == 6)
check("ensure ships exactly one alternating slot",
      len([p for p in fresh["schedule"]["periods"] if "alternates" in p]) == 1)
check("...and it is 4th on A and 5th on B",
      [(a["letter"], a["n"]) for p in fresh["schedule"]["periods"]
       for a in p.get("alternates", [])] == [("A", 4), ("B", 5)])
check("ensure ships start_date null — the pattern is not yet known",
      fresh["schedule"]["ab_cycle"]["start_date"] is None)

kept = {"schedule": {"bedtime": "22:15", "periods": [daily]}}
schedule.ensure(kept)
check("ensure never overwrites a set value",
      kept["schedule"]["bedtime"] == "22:15"
      and len(kept["schedule"]["periods"]) == 1)
check("ensure backfills a missing ab_cycle",
      kept["schedule"]["ab_cycle"]["pattern"] == ["A", "B"])


# ── Migrating the two-period rotation into one slot ────────────────────
#
# The old config said 4th at 10:45 and 5th at 12:20, each teaching a course
# per letter. There is no such day. The migration has to lose something, and
# what it loses is stated in schedule.py rather than chosen here.

old_cfg = {"schedule": {"periods": [
    {"n": 3, "start": "09:50", "end": "10:40", "canvas_course": "208304"},
    {"n": 4, "start": "10:45", "end": "11:35", "alternates": ["A4", "B4"]},
    {"n": 5, "start": "12:20", "end": "13:10", "alternates": ["A5", "B5"]},
    {"n": 6, "start": "13:15", "end": "14:05", "canvas_course": "208670"},
]}}
schedule.ensure(old_cfg)
migrated = old_cfg["schedule"]["periods"]
check("the two rotating periods become ONE slot",
      len(migrated) == 3)
check("the slot keeps the FIRST period's times",
      migrated[1]["start"] == "10:45" and migrated[1]["end"] == "11:35")
check("A is 4th period with 4th's A course",
      migrated[1]["alternates"][0] == {"letter": "A", "n": 4,
                                       "canvas_course": "A4"})
check("B is 5th period with 5th's B course",
      migrated[1]["alternates"][1] == {"letter": "B", "n": 5,
                                       "canvas_course": "B5"})
check("the daily periods around it are untouched",
      migrated[0]["n"] == 3 and migrated[2]["n"] == 6)

again = {"schedule": {"periods": [dict(p) for p in migrated]}}
schedule.ensure(again)
check("migrating an already-migrated config changes nothing",
      again["schedule"]["periods"] == migrated)

lone = {"schedule": {"periods": [
    {"n": 4, "start": "10:45", "end": "11:35", "alternates": ["X", "Y"]}]}}
schedule.ensure(lone)
check("a single legacy rotating period stays one period teaching two courses",
      lone["schedule"]["periods"][0]["alternates"]
      == [{"letter": "A", "n": 4, "canvas_course": "X"},
          {"letter": "B", "n": 4, "canvas_course": "Y"}])

# A migrated slot must be renderable the same day it is migrated — the whole
# reason the migration exists is that the card was showing two.
check("a migrated slot resolves to one period per letter",
      schedule.course_for_period(migrated[1], "A")["label"] == "4"
      and schedule.course_for_period(migrated[1], "B")["label"] == "5")


# ── What the counter is actually worth ───────────────────────────────────────
#
# MEASURED, NOT ASSUMED. The original claim here was that calendar parity
# "breaks the first weekend it meets". It does not: a weekend is two days, an
# even skip, so parity survives it. These assertions record what is true so
# the wrong reason cannot be reintroduced by someone reading the weekend
# cases above and inferring the old story.

from datetime import timedelta                                   # noqa: E402

ANCHOR = date(2026, 8, 10)


def divergences(pattern_len: int, days: int = 400) -> int:
    n = 0
    for i in range(days):
        d = ANCHOR + timedelta(days=i)
        if not schedule.is_school_day(d):
            continue
        if (schedule.school_days_since(ANCHOR, d) % pattern_len
                != (d - ANCHOR).days % pattern_len):
            n += 1
    return n


check("A/B: school-day counting and calendar parity NEVER diverge "
      "(a weekend is an even skip)", divergences(2) == 0)
check("a 3-letter pattern diverges constantly — this is what the counter buys",
      divergences(3) > 100)
check("a 4-letter pattern too", divergences(4) > 100)


def school_days_minus_holidays(start, today, holidays):
    n, d = 0, start
    while d < today:
        if schedule.is_school_day(d) and d not in holidays:
            n += 1
        d += timedelta(days=1)
    return n


# A one-day closure shifts the true count by one, and BOTH methods miss it
# identically. This is the argument for the manual override, and the argument
# against pretending a holiday calendar would be maintained.
LABOR_DAY = {date(2026, 9, 7)}
AFTER_HOLIDAY = date(2026, 9, 8)
check("a one-day holiday desynchronises the weekday counter",
      school_days_minus_holidays(ANCHOR, AFTER_HOLIDAY, LABOR_DAY) % 2
      != schedule.school_days_since(ANCHOR, AFTER_HOLIDAY) % 2)
check("...and calendar parity misses it in exactly the same way",
      schedule.school_days_since(ANCHOR, AFTER_HOLIDAY) % 2
      == (AFTER_HOLIDAY - ANCHOR).days % 2)


# ── passing_period_blocks: derived wake windows ───────────────────────────
#
# DEFAULT_SCHEDULE is exactly the case that first exposed the bug worth
# pinning here: the alternating 4th/5th slot ends at 11:35 and 6th period
# doesn't start until 13:15 — 100 minutes, which is lunch, not a passing
# period. A first cut of this function turned that gap into an hour-long
# wake-hold with no distinction from the five-minute ones around it.

THURSDAY = date(2026, 8, 13)
SATURDAY = date(2026, 8, 15)

_default_blocks = schedule.passing_period_blocks(schedule.DEFAULT_SCHEDULE, THURSDAY)
check("passing_period_blocks finds one block per short gap",
      len(_default_blocks) == 4)
check("...and excludes the 100-minute lunch gap",
      not any(b["start"] == "11:35" for b in _default_blocks))
_first = next(b for b in _default_blocks if b["start"] == "08:50")
check("wake_at is 2 minutes before the block by default",
      _first["wake_at"] == "08:48")
check("a non-school day produces no blocks",
      schedule.passing_period_blocks(schedule.DEFAULT_SCHEDULE, SATURDAY) == [])
check("lead_minutes is configurable",
      schedule.passing_period_blocks(schedule.DEFAULT_SCHEDULE, THURSDAY,
                                     lead_minutes=5)[0]["wake_at"] == "08:45")
check("max_gap_minutes is configurable and can admit the lunch gap",
      any(b["start"] == "11:35" for b in schedule.passing_period_blocks(
          schedule.DEFAULT_SCHEDULE, THURSDAY, max_gap_minutes=200)))

_back_to_back = {"periods": [
    {"n": 1, "start": "08:00", "end": "08:50"},
    {"n": 2, "start": "08:50", "end": "09:40"},   # no gap at all
]}
check("back-to-back periods (no gap) produce no block",
      schedule.passing_period_blocks(_back_to_back, THURSDAY) == [])


# The module decides and acts on nothing — same contract as policy/.
src = Path(schedule.__file__).read_text()
check("schedule.py performs no I/O and reads no clock",
      ".execute(" not in src and "requests" not in src
      and "datetime.now(" not in src and "date.today(" not in src)


if _failures:
    print(f"\n{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("\nall passed")
