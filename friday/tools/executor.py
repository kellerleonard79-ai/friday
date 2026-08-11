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
"""

from __future__ import annotations

import logging
import time
from typing import Any

from tools import ledger as fact_ledger
from tools.registry import ToolSpec
from tools.types import ToolError, ToolOutcome, ToolResult

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


def check_preconditions(spec: ToolSpec, arguments: dict) -> ToolError | None:
    """Every precondition, against this turn's ledger.

    A ledger-less turn fails closed. A precondition exists to assert that
    something was actually read; answering "satisfied" because there is no
    ledger to consult would invert its meaning at exactly the moment the
    plumbing is broken.
    """
    if not spec.preconditions:
        return None

    led = fact_ledger.current()
    if led is None:
        return ToolError(
            kind="precondition_failed",
            message=f"{spec.name} has preconditions but no fact ledger is installed.",
        )

    for pre in spec.preconditions:
        problem = pre.check(led, arguments)
        if problem:
            return ToolError(kind="precondition_failed", message=problem)
    return None


def run(spec: ToolSpec, arguments: dict) -> tuple[ToolOutcome, int]:
    """Validate, check preconditions, execute. Returns (outcome, duration_ms).

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

    error = check_preconditions(spec, cleaned)
    if error is not None:
        return _done(error)

    try:
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

    return _done(outcome)
