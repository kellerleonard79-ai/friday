"""
tests/test_schedule.py
The bell schedule and the A/B rotation. Plain asserts, no test framework.

    python3 tests/test_schedule.py     (from the friday/ package directory)

THE ROTATION IS THE REASON THIS FILE EXISTS. Date parity looks correct for
four days and then meets a weekend, and the failure is silent: the card says
"B day", the user walks into A-day Biology, and nothing anywhere logs an
error. Every weekend and multi-week case below is one that parity gets wrong.
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

# The whole point: Friday A → Monday B. Parity would say Monday is A.
check("MONDAY AFTER A FRIDAY-A IS B (parity says A)",
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
alt = {"n": 4, "start": "10:45", "end": "11:35",
       "alternates": ["209288", "208473"]}
empty = {"n": 2, "start": "08:55", "end": "09:45", "canvas_course": None}

check("a daily period resolves to its course",
      schedule.course_for_period(daily, "A")["course_id"] == "208300")
check("a daily period ignores the letter",
      schedule.course_for_period(daily, "B")["course_id"] == "208300")
check("an unassigned period is a valid state",
      schedule.course_for_period(empty, "A")["course_id"] is None)

check("an alternating period on an A day takes the first course",
      schedule.course_for_period(alt, "A")["course_id"] == "209288")
check("an alternating period on a B day takes the second",
      schedule.course_for_period(alt, "B")["course_id"] == "208473")

unres = schedule.course_for_period(alt, None)
check("an unresolved alternating period picks NEITHER",
      unres["course_id"] is None and unres["resolved"] is False)
check("...and returns both, labeled A and B",
      [x["letter"] for x in unres["alternates"]] == ["A", "B"]
      and [x["course_id"] for x in unres["alternates"]] == ["209288", "208473"])


# ── current_period ───────────────────────────────────────────────────────────

sched = {"periods": schedule.DEFAULT_SCHEDULE["periods"]}


def at(h, m):
    return schedule.current_period(sched, datetime(2026, 8, 12, h, m))


check("before first bell", at(7, 30)["state"] == "before")
check("inside 1st period", at(8, 20)["state"] == "in_period"
      and at(8, 20)["period"]["n"] == 1)
check("the boundary belongs to the NEXT period, not the ending one",
      at(8, 50)["state"] == "passing")
check("passing time knows what is next", at(8, 52)["next"]["n"] == 2)
check("the long gap before 5th is passing time", at(11, 50)["state"] == "passing")
check("inside 7th period", at(14, 30)["period"]["n"] == 7)
check("after the last bell", at(15, 30)["state"] == "after")
check("last period has no next", at(14, 30)["next"] is None)
check("an empty schedule does not crash",
      schedule.current_period({"periods": []}, datetime(2026, 8, 12, 9, 0))
      ["state"] == "no_periods")
check("periods with unparseable times are skipped, not fatal",
      schedule.current_period(
          {"periods": [{"n": 1, "start": "oops", "end": "08:50"}]},
          datetime(2026, 8, 12, 9, 0))["state"] == "no_periods")


# ── ensure() ─────────────────────────────────────────────────────────────────

fresh: dict = {}
schedule.ensure(fresh)
check("ensure adds the block", isinstance(fresh.get("schedule"), dict))
check("ensure ships 7 periods", len(fresh["schedule"]["periods"]) == 7)
check("ensure leaves 4 and 5 alternating",
      all("alternates" in p for p in fresh["schedule"]["periods"]
          if p["n"] in (4, 5)))
check("ensure ships start_date null — the pattern is not yet known",
      fresh["schedule"]["ab_cycle"]["start_date"] is None)

kept = {"schedule": {"bedtime": "22:15", "periods": [daily]}}
schedule.ensure(kept)
check("ensure never overwrites a set value",
      kept["schedule"]["bedtime"] == "22:15"
      and len(kept["schedule"]["periods"]) == 1)
check("ensure backfills a missing ab_cycle",
      kept["schedule"]["ab_cycle"]["pattern"] == ["A", "B"])

# The module decides and acts on nothing — same contract as policy/.
src = Path(schedule.__file__).read_text()
check("schedule.py performs no I/O and reads no clock",
      ".execute(" not in src and "requests" not in src
      and "datetime.now(" not in src and "date.today(" not in src)


if _failures:
    print(f"\n{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("\nall passed")
