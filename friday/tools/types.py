"""
tools/types.py
The tool contract. What a tool is handed, what it may hand back.

The one rule everything else follows from:

    A TOOL RETURNS A ToolResult OR A ToolError. Never a string, never None,
    never a raised exception for an expected failure.

An expected failure is one the model can do something about — a missing
parameter, a range with no events in it, a calendar that does not exist. Those
are ToolError, they go back into the loop as data, and the model gets to react.
A programming error (a bad attribute, a broken import) may still raise: that is
a bug, and swallowing it into a ToolError would hand the model a puzzle instead
of failing where it can be fixed.

Returning a string is the failure this file exists to prevent. A tool that
returns prose has already decided how its answer reads, which means the model
is parsing English to recover data the tool had structured a moment earlier —
and it means the persona is being written in the tool layer, where nothing can
see it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal


class Effect:
    """Something the world should see, produced by a tool and executed above it.

    DELIBERATELY EMPTY. Step 3 is read-only: no tool here produces one, nothing
    executes one, and there is no effects layer behind this class. It exists so
    that ToolResult.effects has a type today and step 4 fills in subclasses
    rather than changing the shape of every result.

    Do not give this behavior. The moment a tool can execute an effect itself,
    invariant 2 is gone and permission cards start arriving after the write
    they were supposed to gate.
    """


# ── Coverage: what a call actually established ───────────────────────────────
#
# COVERAGE IS PART OF THE RETURN CONTRACT, NOT A SIDE EFFECT.
#
# Tools used to record their own reads by reaching for the ledger and calling
# it. Nothing forced them to, and nothing checked that what they recorded
# matched what they read — so a tool that lied, or simply forgot, produced a
# ledger claiming a day had been read when it had not. A precondition that
# trusts the thing it is checking is not a precondition.
#
# Now a tool states what it covered in the value it returns, the executor
# writes the ledger from that, and tool code has no path to the ledger at all
# (see tools/ledger.py — there is no module-level accessor to reach).
#
# Typed rather than a free-form dict so a precondition check is a comparison
# and not a parse. A malformed dict would fail at the moment of checking, which
# in step 4 is the moment before a write.


@dataclass(frozen=True, slots=True)
class CalendarCoverage:
    """A half-open [start, end) day range a call actually read.

    Half-open because that is what the calendar backend window is, and
    converting between inclusive and exclusive in two places is how an
    off-by-one day gets into a calendar write.
    """
    start: date
    end: date

    def covers(self, start: date, end: date) -> bool:
        return self.start <= start and self.end >= end


# Every kind of fact a call can establish. One member today; step 4 adds what a
# write changed. A union rather than a base class with fields, so a precondition
# matching on kind is exhaustive and a new kind is a type error at every site
# that has to care.
Coverage = CalendarCoverage


# Why a tool failed, in a form the turn loop can branch on without reading
# prose. The loop treats these differently:
#
#   missing_parameter    — the model omitted a required argument. Caught in
#                          code before execution (never by asking the prompt
#                          nicely), returned so the model can supply it.
#   precondition_failed  — the tool needed a fact that is not in the ledger.
#                          Two of these in one turn aborts the loop: a model
#                          that cannot satisfy a precondition twice is not
#                          going to on the third try, it is spinning.
#   not_found            — the tool ran and the thing genuinely is not there.
#                          A real answer, not a fault.
#   unavailable          — the backing service failed or timed out. Nothing
#                          the model can fix by rephrasing.
#   invalid_argument     — a parameter was present but unusable (an unparseable
#                          date, an inverted range).
ToolErrorKind = Literal[
    "missing_parameter",
    "precondition_failed",
    "not_found",
    "unavailable",
    "invalid_argument",
]


@dataclass(frozen=True, slots=True)
class ToolError:
    """A failure the model is expected to read and react to.

    `message` IS WRITTEN TO THE MODEL, NOT TO THE USER. It may name parameters,
    fields and internal constraints, because its reader is the thing that will
    retry the call. It must never be surfaced verbatim in a reply — the user
    did not ask about `date_from`, and a leaked tool error reads as Friday
    malfunctioning out loud. The turn loop feeds this back in; the model
    decides what, if anything, the user hears.

    `field` names the offending parameter when there is one. Structured rather
    than embedded in the message so the loop can count and log it without
    parsing English.
    """
    kind: ToolErrorKind
    message: str
    field: str | None = None

    def as_content(self) -> dict[str, Any]:
        """The payload that goes back as a ToolResultTurn."""
        out: dict[str, Any] = {"error": self.kind, "detail": self.message}
        if self.field:
            out["field"] = self.field
        return out


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a successful tool call produced.

    `data` is JSON-able structured data and nothing else. No prose, no
    formatting, no units spelled out in English — see the module docstring.

    `coverage` is what this call established as fact — see the Coverage note
    above. The executor writes it into the turn's ledger; the tool does not.
    A tool that reads nothing returns none, and that is the honest answer.

    `effects` is empty in step 3 and typed for step 4.

    `committed` records whether anything in the world actually changed. It is
    False for every tool in this step, all of which are reads. It exists now
    rather than later because the flag has to be true only when a service has
    confirmed the write back (invariant 4), and a field added later gets
    defaulted to the convenient value at each call site rather than the correct
    one.
    """
    data: dict[str, Any] = field(default_factory=dict)
    coverage: tuple[Coverage, ...] = ()
    effects: tuple[Effect, ...] = ()
    committed: bool = False


# What any tool returns.
ToolOutcome = ToolResult | ToolError
