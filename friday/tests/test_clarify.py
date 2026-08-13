"""
tests/test_clarify.py
The clarification round cap.

    python3 tests/test_clarify.py

Two rounds of asking, then Friday answers with what it has. The specific
failure this exists for: the classifier sees one message and no history, so
the user's ANSWER to a clarifying question is itself a bare fragment — which
is the exact shape that routes to CLARIFY again. Without a cap, Friday asks a
question, receives its answer, and asks the same question about the answer.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory.state as state  # noqa: E402
from router import clarify, plans  # noqa: E402

failures = []


def check(label, cond):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def db():
    conn = sqlite3.connect(":memory:")
    # The real columns, including updated_at: memory/state.py::set writes it,
    # and a table missing it makes every write fail silently — which is
    # exactly what a broken cap looks like from the outside.
    conn.execute("CREATE TABLE system_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return conn


CLARIFY = plans.get("CLARIFY")
ANSWER = plans.get("ANSWER")

print("\n-- two rounds are allowed, the third is not --")
conn = db()
p1, b1 = clarify.guard(conn, CLARIFY)
check("round 1 clarifies", p1 is CLARIFY)
check("round 1 carries the directive", len(b1) == 1 and "ONE short question" in b1[0].content)

p2, b2 = clarify.guard(conn, CLARIFY)
check("round 2 clarifies", p2 is CLARIFY)

p3, b3 = clarify.guard(conn, CLARIFY)
check("round 3 does NOT clarify", p3 is not CLARIFY)
check("round 3 hands back FULL capability, not a toolless plan", p3 is None)
check("round 3 is told to stop asking",
      len(b3) == 1 and "Do not ask a third question" in b3[0].content)
check("round 3 says it as a statement, not another question",
      "as a statement, not a question" in b3[0].content)

print("\n-- and the streak resets, so a fourth clarify is round 1 again --")
p4, b4 = clarify.guard(conn, CLARIFY)
check("the cap did not latch permanently", p4 is CLARIFY)

print("\n-- any non-clarify turn ends the streak --")
conn = db()
clarify.guard(conn, CLARIFY)
clarify.guard(conn, CLARIFY)
check("the streak is on record", state.get(conn, "clarify_streak") == "2")
clarify.guard(conn, ANSWER)
check("an ANSWER turn cleared it", state.get(conn, "clarify_streak") in (None, ""))
p, _ = clarify.guard(conn, CLARIFY)
check("so the next clarification starts from round 1", p is CLARIFY)

print("\n-- a FALLBACK turn ends it too --")
conn = db()
clarify.guard(conn, CLARIFY)
clarify.guard(conn, CLARIFY)
clarify.guard(conn, None)   # the classifier fell back; CHAT answered
check("a fallback cleared it", state.get(conn, "clarify_streak") in (None, ""))

print("\n-- no directive on the plans that carry none --")
_, blocks = clarify.guard(db(), ANSWER)
check("ANSWER injects nothing", blocks == ())
_, blocks = clarify.guard(db(), None)
check("no plan injects nothing", blocks == ())

print("\n-- no connection is not a crash --")
p, b = clarify.guard(None, CLARIFY)
check("guard survives conn=None", p is CLARIFY and b == ())

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("test_clarify: all checks passed")
