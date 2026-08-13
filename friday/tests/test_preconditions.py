"""
tests/test_preconditions.py
What update and delete must have established. Plain asserts, no framework.

    python3 tests/test_preconditions.py    (from the friday/ package directory)

NEITHER TOOL EXISTS YET. This is the requirement they will be registered
against, tested one step before they arrive — because the precondition
machinery has been built since step 3, has never had a live consumer, and the
step that adds update and delete should not be discovering what a precondition
means while also deciding what an update is.

The tests run against the real executor and a real ledger, not against
check() in isolation: the failure this guards is not "the predicate is wrong",
it is "the predicate is never consulted", which is what happened once already
when the ledger lived in a thread-local the worker pool could not see.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from policy.gating import Action, decide                        # noqa: E402
from tools import preconditions as pre                          # noqa: E402
from tools.executor import check_preconditions                  # noqa: E402
from tools.ledger import Ledger                                 # noqa: E402
from tools.registry import ToolSpec                             # noqa: E402
from tools.types import CalendarRead                            # noqa: E402

_failures: list[str] = []


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        _failures.append(label)


TARGET = date(2026, 8, 14)
OTHER = date(2026, 8, 20)


def spec(name: str, preconditions) -> ToolSpec:
    """A registration-shaped stub. The real update/delete tools do not exist;
    what is under test is the precondition tuple they will be registered
    with, checked by the code that will check it."""
    return ToolSpec(
        name=name, description="", when_to_call="", fn=lambda **k: None,
        parameters=(), scope=("write",), effect="gated_write",
        preconditions=preconditions, timeout_s=20.0,
    )


def ledger_covering(*days) -> Ledger:
    led = Ledger()
    led.record(tuple(CalendarRead(start=d, end=d + timedelta(days=1))
                     for d in days))
    return led


UPDATE = spec("update_calendar_event", pre.UPDATE_CALENDAR_EVENT)
DELETE = spec("delete_calendar_event", pre.DELETE_CALENDAR_EVENT)
ARGS = {pre.TARGET_DAY_FIELD: TARGET.isoformat(), "event_id": "abc123"}


# ── Covered: the read happened, this turn, for this day ──────────────────────

print("\n-- covered --")

for name, s in (("update", UPDATE), ("delete", DELETE)):
    check(f"{name} runs when the target's day was read this turn",
          check_preconditions(s, ARGS, ledger_covering(TARGET)) is None)

# A wider read covers a narrower need — one recorded read spanning the target.
wide = Ledger()
wide.record((CalendarRead(start=date(2026, 8, 10), end=date(2026, 8, 25)),))
check("a week-long read covers a single day inside it",
      check_preconditions(UPDATE, ARGS, wide) is None)


# ── Uncovered ────────────────────────────────────────────────────────────────

print("\n-- uncovered --")

for name, s in (("update", UPDATE), ("delete", DELETE)):
    err = check_preconditions(s, ARGS, Ledger())
    check(f"{name} refuses when nothing was read",
          err is not None and err.kind == "precondition_failed")
    # The message is written TO THE MODEL — it is what will go and satisfy it.
    check(f"{name}'s refusal names the day and the tool that fixes it",
          err is not None and TARGET.isoformat() in err.message
          and "get_schedule" in err.message)

# A read of the WRONG day is the case a boolean has_read_calendar flag cannot
# see, and it is the reason the ledger stores coverage instead of a flag.
err = check_preconditions(UPDATE, ARGS, ledger_covering(OTHER))
check("a read of a different day does not satisfy the precondition",
      err is not None and err.kind == "precondition_failed")

# Two adjacent reads are deliberately NOT stitched into one span — stitching
# is where a gap between them becomes invisible.
split = ledger_covering(TARGET - timedelta(days=1), TARGET + timedelta(days=1))
check("reads either side of the target do not stitch across it",
      check_preconditions(UPDATE, ARGS, split) is not None)

# A ledger-less call fails closed. Answering "satisfied" because there is no
# ledger to consult would invert the precondition's meaning at exactly the
# moment the plumbing is broken — which it once was, silently.
check("no ledger at all fails closed",
      check_preconditions(DELETE, ARGS, None) is not None)


# ── The constraint on the future tool signature ──────────────────────────────

print("\n-- the target day must be an argument --")

# A tool taking only an event_id cannot satisfy this, and it would fail at
# 2am rather than at registration. Written down here so the step that adds the
# tools reads it first.
err = check_preconditions(DELETE, {"event_id": "abc123"}, ledger_covering(TARGET))
check("a call with no target day is refused even when the day WAS read",
      err is not None and pre.TARGET_DAY_FIELD in err.message)

err = check_preconditions(UPDATE, {pre.TARGET_DAY_FIELD: "next Friday"},
                          ledger_covering(TARGET))
check("a non-ISO target day is refused",
      err is not None and "not an ISO date" in err.message)


# ── target_proven: the same question, as a bool for the gate ─────────────────

print("\n-- target_proven --")

check("target_proven is True when the day was read",
      pre.target_proven(ledger_covering(TARGET), ARGS))
check("target_proven is False when nothing was read",
      not pre.target_proven(Ledger(), ARGS))
check("target_proven is False for a different day",
      not pre.target_proven(ledger_covering(OTHER), ARGS))
check("target_proven fails closed on a missing day",
      not pre.target_proven(ledger_covering(TARGET), {"event_id": "x"}))
check("target_proven fails closed on an unparseable day",
      not pre.target_proven(ledger_covering(TARGET),
                            {pre.TARGET_DAY_FIELD: "soon"}))


# ── And the gate, which is a different question ──────────────────────────────

print("\n-- the gate is not the precondition --")

led = ledger_covering(TARGET)

# An update whose target WAS proven and which the user stated: AUTO. This is
# the cell that has never had a live producer, and it is the one the update
# tool will land in.
check("a user-stated update with a proven target is AUTO",
      decide(Action(operation="update", provenance="user_stated",
                    tool="update_calendar_event",
                    target_proven=pre.target_proven(led, ARGS))) == "AUTO")

# Same user, same words, no read: the user stated the CHANGE, not WHICH EVENT.
check("a user-stated update with an unproven target is GATED",
      decide(Action(operation="update", provenance="user_stated",
                    tool="update_calendar_event",
                    target_proven=pre.target_proven(Ledger(), ARGS))) == "GATED")

# A DELETE IS GATED WHOEVER ASKED AND HOWEVER WELL PROVEN. The one cell where
# certainty about intent buys nothing, because the cost of being wrong is data
# that cannot be recovered — Friday cannot know what was in the description of
# the event it just removed.
for provenance in ("user_stated", "inferred"):
    check(f"a {provenance} delete is GATED even with a proven target",
          decide(Action(operation="delete", provenance=provenance,
                        tool="delete_calendar_event",
                        target_proven=pre.target_proven(led, ARGS))) == "GATED")

# Having looked is not permission, and permission is not having looked. A
# delete needs both, and they are enforced by two different mechanisms.
check("a delete carries a precondition AND is gated",
      bool(pre.DELETE_CALENDAR_EVENT)
      and decide(Action("delete", "user_stated", target_proven=True)) == "GATED")


if _failures:
    print(f"{len(_failures)} FAILED: {_failures}")
    sys.exit(1)
print("all passed")
