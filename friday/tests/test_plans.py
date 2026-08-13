"""
tests/test_plans.py
The plan vocabulary. Plain asserts, no test dependency:

    python3 tests/test_plans.py

What is being proved here, and it is one thing above the others: A PLAN
REQUIRING A READ CANNOT REACH ITS WRITE WITHOUT ONE, INDEPENDENT OF WHAT THE
MODEL ASKS FOR. Every other assertion in this file is scaffolding around that.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from router import plans  # noqa: E402

failures = []


def check(label, cond):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("\n── the table ──")
check("five plans", set(plans.names()) == {
    "ANSWER", "READ_THEN_ANSWER", "READ_THEN_WRITE", "WRITE_DIRECT", "CLARIFY"})
check("names() is sorted and stable", plans.names() == tuple(sorted(plans.names())))
# get() raises; resolve() does not.
try:
    plans.get("NOPE")
    check("get() raises on an unknown name", False)
except KeyError:
    check("get() raises on an unknown name", True)

check("resolve() of garbage is None, not a raise", plans.resolve("NOPE") is None)
check("resolve(None) is None", plans.resolve(None) is None)
check("resolve('') is None", plans.resolve("") is None)
check("resolve is case- and space-insensitive",
      plans.resolve("  read_then_write  ") is plans.get("READ_THEN_WRITE"))

print("\n── the read gate: a write cannot run before a read ──")
rtw = plans.get("READ_THEN_WRITE")

check("READ_THEN_WRITE requires a read before a write", rtw.write_requires_read)
check("a write with zero reads recorded is REFUSED",
      plans.write_blocked(rtw, ("write",), 0) is not None)
check("the refusal is a sentence for the model, not a bool",
      isinstance(plans.write_blocked(rtw, ("write",), 0), str)
      and "get_schedule" in plans.write_blocked(rtw, ("write",), 0))
check("a write with one read recorded RUNS",
      plans.write_blocked(rtw, ("write",), 1) is None)
check("a READ is never blocked, even with zero reads recorded",
      plans.write_blocked(rtw, ("read",), 0) is None)
check("a tool carrying both scopes is treated as a write",
      plans.write_blocked(rtw, ("read", "write"), 0) is not None)

# The claim the brief asks for, spelled out: the block does not consult the
# model's request, the tool's arguments, or anything the model can influence.
# Its only inputs are the plan, the tool's registered scope and the ledger's
# read count.
check("WRITE_DIRECT does not gate its write",
      plans.write_blocked(plans.get("WRITE_DIRECT"), ("write",), 0) is None)
check("no plan gates nothing (the fallback path is unchanged)",
      plans.write_blocked(None, ("write",), 0) is None)

print("\n── scope narrowing: the profile is the ceiling ──")
CHAT = ("read", "write")

check("no plan leaves the profile scope untouched",
      plans.effective_scope(None, CHAT) == CHAT)
check("READ_THEN_ANSWER narrows CHAT to reads",
      plans.effective_scope(plans.get("READ_THEN_ANSWER"), CHAT) == ("read",))
check("WRITE_DIRECT narrows CHAT to writes",
      plans.effective_scope(plans.get("WRITE_DIRECT"), CHAT) == ("write",))
check("ANSWER yields no tools",
      plans.effective_scope(plans.get("ANSWER"), CHAT) is None)
check("CLARIFY yields no tools",
      plans.effective_scope(plans.get("CLARIFY"), CHAT) is None)

# The asymmetry. A plan may not widen, ever.
check("a plan CANNOT widen a profile that carries no tools",
      plans.effective_scope(plans.get("READ_THEN_WRITE"), None) is None)
check("a plan CANNOT add a scope the profile lacks",
      plans.effective_scope(plans.get("READ_THEN_WRITE"), ("read",)) == ("read",))
check("an empty intersection is None, never ()",
      plans.effective_scope(plans.get("WRITE_DIRECT"), ("read",)) is None)
check("a plan cannot reach 'internal' — commit_calendar_event stays unreachable",
      "internal" not in (plans.effective_scope(plans.get("READ_THEN_WRITE"), CHAT) or ()))

print("\n── hop budgets: the lower of the two, always ──")
check("no plan leaves the profile budget untouched", plans.effective_hops(None, 3) == 3)
check("a plan cannot buy hops the profile did not budget",
      plans.effective_hops(plans.get("READ_THEN_WRITE"), 1) == 1)
check("a plan can spend fewer than the profile allows",
      plans.effective_hops(plans.get("ANSWER"), 3) == 0)
check("ANSWER and CLARIFY are zero-hop by construction",
      plans.get("ANSWER").max_tool_hops == 0
      and plans.get("CLARIFY").max_tool_hops == 0)

print("\n── directives ──")
check("CLARIFY is the only plan carrying one",
      [n for n in plans.names() if plans.get(n).directive] == ["CLARIFY"])
check("CLARIFY's directive asks for ONE question",
      "ONE short question" in plans.get("CLARIFY").directive)

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("test_plans: all checks passed")
