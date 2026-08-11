"""
agent/turn.py
The turn loop: one user message in, one answer out, with tool calls in between.

    build request -> dispatch -> tool calls? -> execute -> append -> repeat
                             \\
                              no tool calls -> return text

Four properties, each of which is a Phase II failure it exists not to repeat:

  BOUNDED, NOT RECURSIVE. max_tool_hops is a for-loop bound. A recursive
  implementation hides its depth in the stack, and the bound stops being
  something you can read off the profile table.

  ONE DEADLINE FOR THE WHOLE TURN. Set once, here, and threaded into every
  dispatch — hop 3 does not get a fresh budget. llm/dispatch.py takes the min
  of its own profile timeout and the deadline it is handed, so nothing below
  has to cooperate for this to hold.

  MANUAL DISPATCH. The SDK's automatic function calling is disabled in
  llm/providers/gemini.py. It would run this loop itself and return only the
  final text, which hides the hop count, hides intermediate token cost, and
  makes the deadline, per-tool timeouts, the ledger and the tool_calls log all
  unenforceable. Call count and call cost are separate levers and Friday needs
  both.

  TOOLS NEVER MESSAGE THE USER. A tool returns data and, later, effects. This
  loop returns text to the channel and the channel sends it. That is invariant
  2, and it is why permission cards will be able to arrive first in step 4.

SYNCHRONOUS, like dispatch(). The caller runs the whole turn in one executor
thread. An async loop would put a blocking SDK call and a 30-second osascript
read back on the event loop.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass

import memory.activity as activity
from llm.dispatch import dispatch
from llm.types import LLMRequest, LLMResponse, ToolCall, ToolCallTurn, ToolResultTurn
from tools import executor, registry
from tools.ledger import Ledger
from tools.types import ToolError, ToolResult

# Importing the tool modules is what registers them. Done here rather than in
# friday.py so the loop cannot run against an empty registry: a turn that
# silently has no tools looks exactly like a model choosing not to call one.
from tools import calendar_read as _register_calendar_tools  # noqa: F401

logger = logging.getLogger("friday.turn")

# Two precondition failures in one turn ends it. A model that cannot satisfy a
# precondition twice is not going to on the third attempt — it is spinning, and
# every spin costs a model call. Better to stop and let it say what it needs.
_MAX_PRECONDITION_FAILURES = 2

# Tools run here rather than on the caller's thread so a per-tool timeout can
# be enforced. The honest limitation: a timeout stops us WAITING, it does not
# kill the thread — Python cannot. The underlying osascript read carries its
# own subprocess timeout (connectors/apple_calendar.py), which is the thing
# that actually bounds the work. This bounds the turn.
_TOOL_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="friday-tool")


@dataclass(frozen=True, slots=True)
class TurnResult:
    """What the channel needs, and nothing it does not.

    Mirrors LLMResponse's error fields deliberately: the channel already keys
    its replies off error_kind, and a turn that failed should not need a second
    vocabulary for the same conditions.
    """
    text: str = ""
    error_kind: str = "none"
    error_message: str = ""
    hops: int = 0
    tool_calls_made: int = 0
    stopped_on: str = "answer"   # answer | hop_limit | preconditions | error


def _preview(outcome, limit: int = 400) -> str:
    payload = outcome.as_content() if isinstance(outcome, ToolError) else outcome.data
    try:
        return json.dumps(payload, default=str)[:limit]
    except Exception:
        return str(payload)[:limit]


def _outcome_label(outcome) -> str:
    return outcome.kind if isinstance(outcome, ToolError) else "ok"


def _execute(call: ToolCall, deadline: float, ledger: Ledger,
             store: dict) -> tuple[object, int, str]:
    """Run one tool call. Returns (outcome, duration_ms, label).

    A tool the model invented is a ToolError, not a raise: hallucinating a name
    is something the model can recover from once it is told, and the registry's
    own KeyError is reserved for Friday's code asking for a tool that does not
    exist, which is a bug.
    """
    if not registry.has(call.name):
        return (
            ToolError(
                kind="not_found",
                message=(
                    f"There is no tool named {call.name!r}. Available: "
                    f"{', '.join(registry.names())}."
                ),
            ),
            0,
            "unknown_tool",
        )

    spec = registry.get(call.name)
    remaining = deadline - time.monotonic()
    budget = min(spec.timeout_s, remaining)
    if budget <= 0:
        return (
            ToolError(kind="unavailable", message=f"{call.name} was not run: the turn ran out of time."),
            0,
            "timeout",
        )

    started = time.monotonic()
    # The turn's ledger and scratch travel WITH the call rather than being
    # picked up from the worker thread, which cannot see anything the turn
    # installed on its own thread.
    future = _TOOL_POOL.submit(
        executor.run, spec, dict(call.arguments), ledger=ledger, store=store
    )
    try:
        outcome, duration_ms = future.result(timeout=budget)
    except FutureTimeout:
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            f"Tool {call.name} exceeded its {budget:.0f}s budget; abandoning the "
            f"wait. The worker thread may still be running."
        )
        return (
            ToolError(kind="unavailable", message=f"{call.name} timed out."),
            duration_ms,
            "timeout",
        )
    return outcome, duration_ms, _outcome_label(outcome)


def run_turn(request: LLMRequest, conn=None) -> TurnResult:
    """One full turn. Never raises for an LLM- or tool-level failure."""
    profile = request.profile
    deadline = time.monotonic() + profile.timeout_s

    # Per-turn state, owned here and passed down. The scratch holds tool
    # payload (the calendar read cache); the ledger holds coverage and is
    # written only by the executor, from what a tool returned.
    #
    # Plain objects rather than thread-locals: tools execute in a worker pool,
    # so anything installed on this thread is invisible from inside a tool.
    ledger = Ledger()
    store: dict = {}

    tool_turns: list = []
    precondition_failures = 0
    calls_made = 0
    hop = 0

    try:
        # max_tool_hops counts ROUNDS OF TOOL EXECUTION, not dispatches. A turn
        # that uses every hop therefore makes max_tool_hops + 1 model calls: one
        # per round, plus the one that produces the answer. The range bound is a
        # belt-and-braces stop — every real exit below is a return — because an
        # unbounded while-loop around a paid API call is not a thing to leave
        # lying around.
        for _ in range(profile.max_tool_hops + 2):
            if time.monotonic() >= deadline:
                logger.warning(f"Turn deadline exhausted after {hop} hop(s).")
                return TurnResult(
                    error_kind="network",
                    error_message="the turn ran out of time",
                    hops=hop, tool_calls_made=calls_made, stopped_on="error",
                )

            response = dispatch(dataclasses.replace(
                request, deadline=deadline, tool_turns=tuple(tool_turns)
            ))

            # No tool calls means this is the answer — the ordinary exit, and
            # the only one on a profile with tool_scope=None.
            if response.finish == "error" or not response.tool_calls:
                return _finish(response, hop, calls_made, "answer")

            hop += 1
            # The model's request goes into the transcript before its results,
            # always. An unanswered tool call is a shape the next dispatch
            # rejects, so every path from here must append a result for each.
            tool_turns.append(ToolCallTurn(calls=response.tool_calls))

            if hop > profile.max_tool_hops:
                logger.warning(
                    f"Hop limit ({profile.max_tool_hops}) reached with tool calls "
                    f"outstanding: {[c.name for c in response.tool_calls]}"
                )
                for call in response.tool_calls:
                    tool_turns.append(ToolResultTurn(
                        name=call.name,
                        content={
                            "error": "hop_limit",
                            "detail": "No further tool calls are available this "
                                      "turn. Answer with what you already have.",
                        },
                        is_error=True,
                    ))
                final = dispatch(dataclasses.replace(
                    request, deadline=deadline, tool_turns=tuple(tool_turns)
                ))
                return _finish(final, hop, calls_made, "hop_limit")

            for call in response.tool_calls:
                calls_made += 1
                outcome, duration_ms, label = _execute(call, deadline, ledger, store)

                if isinstance(outcome, ToolError) and outcome.kind == "precondition_failed":
                    precondition_failures += 1

                tool_turns.append(ToolResultTurn(
                    name=call.name,
                    content=(outcome.as_content() if isinstance(outcome, ToolError)
                             else outcome.data),
                    is_error=isinstance(outcome, ToolError),
                ))
                _log_call(conn, request, call, outcome, duration_ms, hop, label)

            if precondition_failures >= _MAX_PRECONDITION_FAILURES:
                logger.warning(
                    f"{precondition_failures} precondition failures in one turn — "
                    f"stopping and letting the model say what it needs."
                )
                final = dispatch(dataclasses.replace(
                    request, deadline=deadline, tool_turns=tuple(tool_turns)
                ))
                return _finish(final, hop, calls_made, "preconditions")

        # Unreachable: every branch above returns. Kept because "unreachable"
        # and "never happens" are different claims, and a silent None here
        # would surface as an empty reply.
        logger.error("Turn loop fell through its bound — this is a bug.")
        return TurnResult(
            error_kind="fatal",
            error_message="the turn loop did not terminate cleanly",
            hops=hop, tool_calls_made=calls_made, stopped_on="error",
        )

    finally:
        # Both die with the turn. A calendar read must not survive into the
        # next message: the user may well have changed the calendar because of
        # what Friday just said.
        store.clear()
        ledger.entries.clear()


def _finish(response: LLMResponse | None, hops: int, calls: int,
            stopped_on: str) -> TurnResult:
    if response is None:
        return TurnResult(
            error_kind="fatal",
            error_message="the turn produced no model response",
            hops=hops, tool_calls_made=calls, stopped_on="error",
        )
    return TurnResult(
        text=response.text,
        error_kind=response.error_kind,
        error_message=response.error_message,
        hops=hops,
        tool_calls_made=calls,
        stopped_on="error" if response.finish == "error" else stopped_on,
    )


def _log_call(conn, request: LLMRequest, call: ToolCall, outcome,
              duration_ms: int, hop: int, label: str) -> None:
    """One tool_calls row. Wrapped — instrumentation never fails a turn."""
    try:
        activity.record_tool_call(
            conn,
            tool_name=call.name,
            args_json=json.dumps(dict(call.arguments), default=str),
            result_preview=_preview(outcome),
            duration_ms=duration_ms,
            triggered_by=request.triggered_by,
            hop=hop,
            outcome=label,
        )
    except Exception as e:
        logger.debug(f"tool_call logging failed: {e}")
