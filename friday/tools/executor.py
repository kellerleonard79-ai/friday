"""
tools/executor.py
The gate between a model's request and a tool actually running.

VALIDATION IS IN CODE, NOT IN THE PROMPT. Phase II asked the model, in prose,
to gather required arguments before calling and to ask the user when something
was missing. That works most of the time. Failing sometimes, silently, is the
problem: the tool ran on a default nobody chose, returned a plausible answer
about the wrong day, and nothing anywhere recorded that a parameter had been
invented.

A required parameter is one with no default in the tool's signature — the same
derivation tools/registry.py used to build the schema the model was shown, so
the check and the contract cannot drift.

A missing required parameter produces a ToolError of kind missing_parameter
naming the field, and it goes back INTO THE LOOP. It is never surfaced to the
user directly: the model is what will supply the value or ask for it, and a
raw "date_from is required" in a Telegram reply reads as Friday malfunctioning
out loud.

THIS MODULE IS THE LEDGER'S ONLY WRITER. A read tool declares what it covered
in the value it returns and this file records that. A WRITE tool declares
nothing: this file synthesises the record from the service's own confirmation,
and derives `committed` from the same object, so the two cannot disagree. Tools
have no path to the ledger — it is an ordinary object passed in here, not a
global they could reach.

The "a failed call records nothing" rule splits along the same line. A failed
read is not a read. A failed WRITE may have been committed server-side, and
records an attempt so a retry has something to check. See _record().
"""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from tools import scratch
from tools.ledger import Ledger
from tools.registry import ToolSpec
from tools.types import (CalendarWrite, ToolError, ToolOutcome, ToolResult,
                         WriteAttempt)

logger = logging.getLogger("friday.tools.executor")

# What the model may send for a given JSON type, and how to land it on Python.
# Kept narrow: a string "60" for an integer is worth accepting because models
# produce it routinely and the intent is unambiguous. Anything genuinely
# ambiguous is an invalid_argument the model can see and correct.
_TRUE = ("true", "yes", "1")
_FALSE = ("false", "no", "0")


def _coerce(value: Any, json_type: str) -> Any:
    """Land a model-supplied value on its declared Python type.

    bool is handled by hand because bool("false") is True — the single most
    dangerous coercion in Python, and one a model triggers routinely by sending
    a JSON string where it meant a JSON boolean. Nothing here uses a boolean
    parameter yet; the trap is disarmed before something does.
    """
    if json_type == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"{value!r} is not a boolean")
    if json_type == "integer":
        # isinstance(True, int) is True, so bools have to be refused explicitly
        # or a stray `true` silently becomes 1.
        if isinstance(value, bool):
            raise ValueError(f"{value!r} is a boolean, not an integer")
        return int(value)
    if json_type == "number":
        if isinstance(value, bool):
            raise ValueError(f"{value!r} is a boolean, not a number")
        return float(value)
    if json_type == "string":
        return str(value)
    # array/object pass through — the model's JSON shape is already right.
    return value


def validate(spec: ToolSpec, arguments: dict) -> tuple[dict, ToolError | None]:
    """(cleaned arguments, error). A non-None error means do not run the tool."""
    declared = {p.name: p for p in spec.parameters}

    for name in spec.required:
        if name not in arguments or arguments[name] is None:
            return {}, ToolError(
                kind="missing_parameter",
                message=(
                    f"{spec.name} requires {name}, which was not provided. "
                    f"Supply it, or ask the user for it if you cannot determine it."
                ),
                field=name,
            )

    cleaned: dict[str, Any] = {}
    for name, value in arguments.items():
        param = declared.get(name)
        if param is None:
            # Dropped rather than rejected. An extra key is usually the model
            # restating something harmlessly, and spending a whole hop telling
            # it so costs more than ignoring it — while passing it through
            # would raise TypeError inside the tool, which is a crash rather
            # than an outcome. Logged because a recurring one means the schema
            # and the model disagree about something.
            logger.warning(
                f"{spec.name}: dropping unknown argument {name!r}. "
                f"Declared: {', '.join(sorted(declared)) or '<none>'}"
            )
            continue
        try:
            cleaned[name] = _coerce(value, param.json_type)
        except (TypeError, ValueError):
            return {}, ToolError(
                kind="invalid_argument",
                message=(
                    f"{spec.name}: {name}={value!r} is not a {param.json_type}."
                ),
                field=name,
            )

    return cleaned, None


def check_preconditions(spec: ToolSpec, arguments: dict,
                        ledger: Ledger | None) -> ToolError | None:
    """Every precondition, against this turn's ledger.

    A ledger-less call fails closed. A precondition exists to assert that
    something was actually established; answering "satisfied" because there is
    no ledger to consult would invert its meaning at exactly the moment the
    plumbing is broken — which it once was, silently, when the ledger lived in
    a thread-local the worker pool could not see.
    """
    if not spec.preconditions:
        return None

    if ledger is None:
        return ToolError(
            kind="precondition_failed",
            message=f"{spec.name} has preconditions but no fact ledger was provided.",
        )

    for pre in spec.preconditions:
        problem = pre.check(ledger, arguments)
        if problem:
            return ToolError(kind="precondition_failed", message=problem)
    return None


def run(spec: ToolSpec, arguments: dict, *, ledger: Ledger | None = None,
        store: dict | None = None) -> tuple[ToolOutcome, int]:
    """Validate, check preconditions, execute, write the ledger.
    Returns (outcome, duration_ms).

    `ledger` and `store` are passed rather than reached for. This function runs
    on a worker thread, so anything ambient the turn set up on its own thread
    would be invisible here — a thread-local ledger was, and every precondition
    failed closed because of it.

    SYNCHRONOUS. The caller runs this in an executor — a calendar read costs
    tens of seconds on the JXA path and must never touch the event loop.

    An unexpected exception is caught and returned as `unavailable` rather than
    propagated. A tool that raises is a bug, but a bug inside one tool must not
    take down the turn that called it: the model can be told the tool failed
    and answer without it, whereas an exception escaping here surfaces as a
    dead conversation. The traceback goes to the log, where it can be fixed.
    """
    started = time.monotonic()

    def _done(outcome: ToolOutcome) -> tuple[ToolOutcome, int]:
        return outcome, int((time.monotonic() - started) * 1000)

    cleaned, error = validate(spec, arguments)
    if error is not None:
        return _done(error)

    error = check_preconditions(spec, cleaned, ledger)
    if error is not None:
        return _done(error)

    try:
        # The tool's own per-turn storage is installed only for the length of
        # this call, and only the scratch — never the ledger.
        with scratch.installed(store):
            outcome = spec.fn(**cleaned)
    except Exception as e:
        logger.exception(f"Tool {spec.name} raised")
        return _done(ToolError(
            kind="unavailable",
            message=f"{spec.name} failed: {type(e).__name__}: {e}",
        ))

    if not isinstance(outcome, (ToolResult, ToolError)):
        # The contract in tools/types.py, enforced. A tool returning a string
        # would otherwise be JSON-serialized into the transcript and read as
        # data, which is how prose gets back into the loop unnoticed.
        logger.error(
            f"Tool {spec.name} returned {type(outcome).__name__}, not a "
            f"ToolResult or ToolError."
        )
        return _done(ToolError(
            kind="unavailable",
            message=f"{spec.name} returned a malformed result.",
        ))

    outcome = _record(spec, outcome, ledger)
    return _done(outcome)


def _record(spec: ToolSpec, outcome: ToolOutcome,
            ledger: Ledger | None) -> ToolOutcome:
    """Write the ledger from what the call produced, and — for a write —
    settle `committed` at the same time.

    THE RULE IS DIFFERENT FOR READS AND WRITES, AND THE DIFFERENCE IS THE POINT.

    A READ declares what it covered and the executor believes it. That is the
    step-3 contract and it is unchanged. A failed read records nothing: a read
    that did not happen is not a read, and crediting one would let a
    precondition pass on the strength of a call that never got an answer.

    A WRITE DECLARES NOTHING. The executor synthesises the record from the
    WriteConfirmation the tool carried up from the service, and derives
    `committed` from that same object. This is what makes it impossible for
    the two to disagree — a tool cannot report committed=True alongside a
    record saying the write is in doubt, because it does not write either one.
    Anything a write tool puts in `records` is discarded, deliberately.

    AND A FAILED WRITE IS NOT LIKE A FAILED READ. This is the split. A write
    that timed out or died on the socket may have been committed server-side:
    the request was already out. Recording nothing would leave a retry with
    nothing to check against, so it would have to go and ask the service —
    the same service that just failed to answer. That is precisely how a
    client-side timeout on a server-side success turns into two events on a
    calendar. So an `unknown` write records a WriteAttempt carrying its
    fingerprint, whether it arrived as a ToolResult or a ToolError.

    A `refused` write records nothing, because refused means the service told
    us it did not happen.
    """
    if spec.effect != "write":
        if ledger is not None and isinstance(outcome, ToolResult) and outcome.records:
            ledger.record(outcome.records)
        return outcome

    confirmation = outcome.write
    if confirmation is None:
        # A write tool that came back without one. Not fatal — a write can fail
        # before it attempts anything (a missing parameter, an unresolvable
        # calendar) and there is genuinely nothing to record. Logged at info
        # for a ToolResult, which is the shape that should not happen: a write
        # that claims success has to say what the service said.
        if isinstance(outcome, ToolResult):
            logger.error(
                f"Write tool {spec.name} returned a ToolResult with no "
                f"WriteConfirmation. Reporting it as uncommitted."
            )
            return dataclasses.replace(outcome, records=(), committed=False)
        return outcome

    if confirmation.status == "written":
        entry = CalendarWrite(
            identifier=confirmation.identifier,
            day=confirmation.day,
            fingerprint=confirmation.fingerprint,
            verified=confirmation.verified,
        )
    elif confirmation.status == "unknown":
        entry = WriteAttempt(
            fingerprint=confirmation.fingerprint,
            day=confirmation.day,
            detail=confirmation.detail,
        )
    else:  # refused — the service said it did not happen
        entry = None

    if ledger is not None and entry is not None:
        ledger.record((entry,))

    if isinstance(outcome, ToolResult):
        # committed is true only for `written`, and only the executor sets it.
        return dataclasses.replace(
            outcome, records=(entry,) if entry is not None else (),
            committed=confirmation.status == "written",
        )
    return outcome
