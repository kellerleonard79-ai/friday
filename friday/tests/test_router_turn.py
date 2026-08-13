"""
tests/test_router_turn.py
The turn loop under a plan.

    python3 tests/test_router_turn.py

THE CLAIM BEING PROVED: a READ_THEN_WRITE turn cannot reach its write without
a read, REGARDLESS OF WHAT THE MODEL ASKS FOR. tests/test_plans.py proves the
predicate; this proves the loop actually consults it, with a model stubbed to
do the wrong thing as insistently as it can.

The model here is a list of canned responses, and that is the point. A real
model usually behaves — which is exactly how "the model usually stays quiet"
came to be holding up an invariant on its own for a step and a half in step 4.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.turn as turn  # noqa: E402
from llm.types import LLMRequest, LLMResponse, Profile, ToolCall, Usage  # noqa: E402
from router import plans  # noqa: E402

failures = []


def check(label, cond):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


CHAT = Profile(
    name="CHAT", model="stub",
    tool_scope=("read", "write"), max_tool_hops=3, timeout_s=30.0,
)


def stub_dispatch(responses):
    """Replace the dispatcher with a fixed script, and record what it saw."""
    seen = []

    def fake(request):
        seen.append(request)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    turn.dispatch = fake
    return seen


def call(name, **args):
    return ToolCall(name=name, arguments=args)


def resp(text="", calls=()):
    return LLMResponse(
        text=text, usage=Usage(model="stub"),
        finish="tool_calls" if calls else "stop",
        tool_calls=tuple(calls),
    )


WRITE = dict(title="Team dinner", date="2026-08-24", start_time="18:30")

print("\n-- READ_THEN_WRITE: the model asks to write, twice, with no read --")

seen = stub_dispatch([
    resp(calls=[call("add_calendar_event", **WRITE)]),
    resp(calls=[call("add_calendar_event", **WRITE)]),
    resp(text="I need to check the calendar first, sir."),
])
result = turn.run_turn(
    LLMRequest(profile=CHAT, prompt="add team dinner on August 24th at 6:30pm"),
    conn=None, plan=plans.get("READ_THEN_WRITE"),
)

check("the write NEVER ran - no effect was produced", result.effects == ())
check("no tool call was counted as made", result.tool_calls_made == 0)
check("the turn stopped on plan_refused", result.stopped_on == "plan_refused")
check("the model was told what was missing, not just refused",
      any("get_schedule" in str(getattr(t, "content", "")) for t in seen[-1].tool_turns))
check("the refusal is marked as an error turn so the model reads it as one",
      any(getattr(t, "is_error", False) for t in seen[-1].tool_turns))
check("it gave up after two refusals rather than spinning", len(seen) == 3)

print("\n-- READ_THEN_WRITE: read first, and the write is allowed through --")
# add_calendar_event really runs here and really tries to stage a card, against
# conn=None. It fails at the insert, loudly, and that is fine: the assertion is
# that the GATE OPENED, and nothing on this path writes to a calendar — the
# card is a proposal and the write happens on a tap.
seen = stub_dispatch([
    resp(calls=[call("get_schedule", date_from="2026-08-24", date_to="2026-08-24")]),
    resp(calls=[call("add_calendar_event", **WRITE)]),
    resp(text="Done, sir."),
])
result = turn.run_turn(
    LLMRequest(profile=CHAT, prompt="add team dinner on August 24th at 6:30pm"),
    conn=None, plan=plans.get("READ_THEN_WRITE"),
)
check("the turn did NOT stop on plan_refused", result.stopped_on != "plan_refused")
check("both calls were attempted", result.tool_calls_made == 2)

print("\n-- the plan governs scope and hops on the request the model sees --")
seen = stub_dispatch([resp(text="Nothing on today, sir.")])
turn.run_turn(LLMRequest(profile=CHAT, prompt="what's on today"),
              conn=None, plan=plans.get("READ_THEN_ANSWER"))
check("READ_THEN_ANSWER narrowed the request to reads",
      seen[0].tool_scope == ("read",))
check("the plan name rode along for the exchange log",
      seen[0].plan_name == "READ_THEN_ANSWER")

seen = stub_dispatch([resp(text="Hello, sir.")])
turn.run_turn(LLMRequest(profile=CHAT, prompt="hello"),
              conn=None, plan=plans.get("ANSWER"))
check("ANSWER carries no tools at all", seen[0].tool_scope is None)

print("\n-- plan=None is the old path, untouched --")
seen = stub_dispatch([resp(text="Right away, sir.")])
turn.run_turn(LLMRequest(profile=CHAT, prompt="anything"), conn=None, plan=None)
check("no plan leaves tool_scope at the sentinel (the profile decides)",
      not isinstance(seen[0].tool_scope, (tuple, type(None))))
check("no plan leaves plan_name empty", seen[0].plan_name == "")

seen = stub_dispatch([
    resp(calls=[call("add_calendar_event", **WRITE)]),
    resp(text="Done, sir."),
])
result = turn.run_turn(LLMRequest(profile=CHAT, prompt="add team dinner"),
                       conn=None, plan=None)
check("with no plan the write is attempted, exactly as before the router",
      result.tool_calls_made == 1 and result.stopped_on != "plan_refused")

print()
if failures:
    print(f"FAILED: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("test_router_turn: all checks passed")
