"""
router/
What to do with a message before spending a model call on it.

Three tiers, in order:

  1. router/fastpath.py  — pattern match, no model, works offline
  2. router/classify.py  — one cheap CLASSIFY call returning a plan shape
  3. no plan at all      — today's behavior, CHAT with its own tool scope

TIER 3 IS THE FALLBACK AND IT IS DELIBERATELY NOT A PLAN. A malformed
classifier response, an unreachable classifier, an unknown plan name: all of
them return None, and `agent/turn.py::run_turn(request, conn, plan=None)` is
byte-for-byte the path that ran before this package existed. The router may
narrow what a turn is allowed to do; it may never be the reason a turn can do
less than it could yesterday, and the cheapest way to guarantee that is for
the failure mode to be the old code rather than a plan that approximates it.
"""
