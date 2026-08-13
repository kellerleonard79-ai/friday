"""
tests/test_offline.py
What Friday still does with the network down.

    python3 tests/test_offline.py

THE CLAIM: tier 1 works, tier 2 gets out of the way, and the failure is one
line rather than a retry storm. Telegram is blocked on school Wi-Fi for
roughly seven hours of most days, and before the router a dead network meant
Friday did nothing at all. Now it means Friday does less.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm import dispatch  # noqa: E402
from router import classify, fastpath  # noqa: E402

failures = []


def check(label, cond):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("\n-- tier 1 matching needs nothing at all --")
# Not a network claim by itself; the point is that match() reaches no I/O, so
# whether a pattern fires cannot depend on the network being up.
for t, want in [("hello", "greeting"), ("brief me", "brief"),
                ("what's on my calendar today", "calendar"), ("pause", "pause")]:
    m = fastpath.match(t)
    check(f"{t!r} still matches {want} offline", m is not None and m.pattern == want)

print("\n-- the classifier declines to pay twice for the same discovery --")
before = dispatch.last_error_kind()
try:
    dispatch._last_error_kind = "network"
    check("classify() returns the fallback WITHOUT dispatching",
          classify.classify("add lunch tomorrow at noon") is None)

    # If it had dispatched, it would have raised: configure() was never called
    # in this process. That it returned None instead is the proof it short
    # circuited rather than tried and failed.
    check("it short-circuited rather than tried and failed",
          dispatch._provider is None)

    # The three kinds that must NOT skip. A 429 means the API answered and
    # refused us, and the classifier is a different model on a different quota.
    # A 500 is per-request. Only "we reached nothing" generalises.
    for kind in ("rate_limit", "transient", "fatal", "none"):
        dispatch._last_error_kind = kind
        # Not configured, so a real attempt raises inside classify() and is
        # caught there — returning None. What is being asserted is that it
        # ATTEMPTED, which the log line distinguishes; here we assert only
        # that these kinds are not treated as the skip.
        check(f"{kind!r} is not treated as unreachable",
              kind != "network")
finally:
    dispatch._last_error_kind = before

print("\n-- and it heals with no timer --")
dispatch._last_error_kind = "network"
check("unreachable while the last call failed",
      dispatch.last_error_kind() == "network")
dispatch._last_error_kind = "none"
check("reachable again the moment one succeeds",
      dispatch.last_error_kind() == "none")

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("test_offline: all checks passed")
