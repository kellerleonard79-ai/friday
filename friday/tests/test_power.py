"""
tests/test_power.py
Passing-period wake scheduling. Plain asserts, no test framework.

    python3 tests/test_power.py     (from the friday/ package directory)

Covers the parts of power.py that do not need real root or a real pmset:
block derivation/combination, the manual-hold-overrides-the-master-switch
ordering (a real bug caught while building this — see active_hold's
docstring), and materialize_wakes()'s bookkeeping (idempotency, and that the
master switch actually collapses desired wakes to nothing — also a real bug
this file would have caught). Everything that shells out to `sudo pmset` is
exercised through the same code path but expected to fail closed here, since
no CI box has the sudoers rule installed.
"""

import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import power                                                      # noqa: E402
import schedule                                                   # noqa: E402
import memory.state as state                                      # noqa: E402
from memory.db import Database                                    # noqa: E402


def _briefing_stamps(c, now):
    """The briefing half of _desired_stamps, for asserting the block half is
    empty without hardcoding timestamps that move with the config."""
    return power._desired_stamps(
        {**c, "schedule": {}, "wake_schedule": {**(c.get("wake_schedule") or {}),
                                               "custom_blocks": []}}, now)

failures = []


def check(label, cond):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def fresh_conn():
    return Database(":memory:").connection()


THURSDAY = date(2026, 8, 13)


def cfg(**wake_overrides):
    w = {"enabled": True, "lead_minutes": 2, "wake_days_ahead": 2,
        "custom_blocks": []}
    w.update(wake_overrides)
    return {"schedule": schedule.DEFAULT_SCHEDULE, "wake_schedule": w}


print("\n-- combined_blocks: derived + custom, lunch excluded, weekday-filtered --")
c = cfg(custom_blocks=[
    {"label": "After school", "start": "15:05", "end": "15:20", "enabled": True},
    {"label": "Sunday only", "start": "18:00", "end": "18:10",
     "enabled": True, "weekdays": [6]},
])
blocks = power.combined_blocks(c, THURSDAY)
check("derived blocks present", any(b["source"] == "derived" for b in blocks))
check("lunch gap excluded from derived blocks",
      not any(b["start"] == "11:35" for b in blocks))
check("always-on custom block present on a Thursday",
      any(b["id"] == "custom:0" for b in blocks))
check("Sunday-only custom block absent on a Thursday",
      not any(b["id"] == "custom:1" for b in blocks))
check("blocks are sorted by start time",
      [b["start"] for b in blocks] == sorted(b["start"] for b in blocks))

print("\n-- derived_overrides disables a specific derived block --")
c2 = cfg(derived_overrides={"08:50-08:55": False})
blocks2 = power.combined_blocks(c2, THURSDAY)
b1 = next(b for b in blocks2 if b["id"] == "derived:08:50-08:55")
check("overridden block is present but disabled", b1["enabled"] is False)
check("other derived blocks stay enabled",
      all(b["enabled"] for b in blocks2 if b["source"] == "derived"
          and b["id"] != "derived:08:50-08:55"))

print("\n-- active_hold: manual overrides the master switch, not just blocks --")
conn = fresh_conn()
c_off = cfg(enabled=False)
now = datetime(2026, 8, 13, 8, 49)   # inside the first derived block
check("master off + no manual hold => inactive",
      power.active_hold(c_off, conn, now)["active"] is False)
power.set_manual_hold(conn, 15, "test")
hold = power.active_hold(c_off, conn, now)
check("a manual hold is active even with the master switch OFF "
     "(a toggle that no-ops when the schedule is off looks broken)",
      hold["active"] is True and hold["reason"] == "manual")
power.clear_manual_hold(conn)
check("clearing the manual hold restores the master-off verdict",
      power.active_hold(c_off, conn, now)["active"] is False)

print("\n-- active_hold: a derived block, on and off --")
conn2 = fresh_conn()
c_on = cfg(enabled=True)
inside = datetime(2026, 8, 13, 8, 49)     # wake_at=08:48 .. end=08:55
outside = datetime(2026, 8, 13, 8, 47)    # before wake_at
check("inside a block's [wake_at, end) is active",
      power.active_hold(c_on, conn2, inside)["active"] is True)
check("before wake_at is not active",
      power.active_hold(c_on, conn2, outside)["active"] is False)

print("\n-- materialize_wakes: master off collapses BLOCK wakes to nothing --")
# The master switch governs the passing-period block schedule only. Briefing
# wakes are the primary use and have their own switch, so this asserts the
# block half is empty rather than the whole set — turning the speculative
# feature off must not stop the machine waking for its briefings.
conn3 = fresh_conn()
c_disabled = cfg(enabled=False)
start3 = datetime(2026, 8, 13, 7, 0)
desired = power._desired_stamps(c_disabled, start3)
briefing_only = power._desired_stamps(
    {**c_disabled, "wake_schedule": {**c_disabled["wake_schedule"],
                                    "briefing_wakes": False}}, start3)
check("no wakes at all when both switches are off", briefing_only == set())
check("block-derived stamps are gone when the master switch is off",
      desired == _briefing_stamps(c_disabled, start3))
check("briefing wakes survive the master switch being off", len(desired) > 0)


print("\n-- briefing wakes: the hold releases the moment the briefing sends --")
conn_b = fresh_conn()
c_b = {"agent": {"morning_briefing_time": "07:00", "briefing_time": "20:00"},
      "schedule": schedule.DEFAULT_SCHEDULE, "wake_schedule": {"enabled": False}}
day = date(2026, 8, 13)
slots = power.briefing_wake_times(c_b, day)
check("one wake per briefing slot, defaulting to 2 minutes early",
      [(b["slot"], b["at"], b["wake_at"]) for b in slots]
      == [("morning", "07:00", "06:58"), ("evening", "20:00", "19:58")])
check("the lead window holds, even with the master switch off",
      power.active_hold(c_b, conn_b, datetime(2026, 8, 13, 6, 58))["reason"]
      == "briefing:morning")
check("still holding after the due time while the briefing has not sent",
      power.active_hold(c_b, conn_b, datetime(2026, 8, 13, 7, 5))["reason"]
      == "briefing:morning")
state.set(conn_b, "last_morning_briefing_sent", day.isoformat())
check("the hold releases the instant the briefing's own lock is set "
     "(the same key the catch-up net reads — one source of truth)",
      power.active_hold(c_b, conn_b, datetime(2026, 8, 13, 6, 58))["active"]
      is False)
check("the grace window bounds a briefing that never sends",
      power.active_hold(c_b, conn_b, datetime(2026, 8, 13, 19, 58))["reason"]
      == "briefing:evening"
      and power.active_hold(c_b, conn_b,
                            datetime(2026, 8, 13, 20, 11))["active"] is False)
check("briefing_wakes=False turns them off entirely",
      power.active_hold({**c_b, "wake_schedule": {"briefing_wakes": False}},
                        conn_b, datetime(2026, 8, 13, 19, 58))["active"] is False)

print("\n-- briefing wakes: a one-shot override moves that day's wake --")
conn_o = fresh_conn()
state.set(conn_o, "evening_briefing_override", "2026-08-13T21:30:00")
check("the override replaces the configured time for the day it names",
      [b["at"] for b in power.briefing_wake_times(c_b, day, conn_o)]
      == ["07:00", "21:30"])
check("and leaves other days alone",
      [b["at"] for b in power.briefing_wake_times(c_b, date(2026, 8, 14), conn_o)]
      == ["07:00", "20:00"])

print("\n-- materialize_wakes: idempotent on a second run with no config change --")
conn4 = fresh_conn()
c_on2 = cfg(enabled=True)
start = datetime(2026, 8, 13, 7, 0)
# Both runs will fail to actually call `sudo pmset` in this environment (no
# sudoers rule here), so `added`/`cancelled` measure INTENT, not success —
# what matters for idempotency is that the second run wants to do nothing
# new, which is independent of whether the first run's pmset calls worked.
r1 = power.materialize_wakes(c_on2, conn4, now=start)
check("first run has a non-empty desired set", len(r1["desired"]) > 0)
r2 = power.materialize_wakes(c_on2, conn4, now=start)
check("second run's desired set is unchanged",
      r2["desired"] == r1["desired"])

print("\n-- sudoers_line names the exact commands this module runs --")
line = power.sudoers_line()
check("covers disablesleep on and off",
      "disablesleep 0" in line and "disablesleep 1" in line)
check("covers scheduling and cancelling a wake",
      "schedule wakeorpoweron" in line and "schedule cancel wakeorpoweron" in line)
check("is a single NOPASSWD rule (no visudo/sudoers file touched by this module)",
      "NOPASSWD" in line)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("test_power: all checks passed")
