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


# ── Records: what a call actually established ────────────────────────────────
#
# A RECORD IS PART OF THE RETURN CONTRACT, NOT A SIDE EFFECT.
#
# Tools used to record their own reads by reaching for the ledger and calling
# it. Nothing forced them to, and nothing checked that what they recorded
# matched what they read — so a tool that lied, or simply forgot, produced a
# ledger claiming a day had been read when it had not. A precondition that
# trusts the thing it is checking is not a precondition.
#
# Now a tool states what it established in the value it returns, the executor
# writes the ledger from that, and tool code has no path to the ledger at all
# (see tools/ledger.py — there is no module-level accessor to reach).
#
# It was called `coverage` while reads were the only kind. It is `records` now
# because the ledger holds writes too, and "coverage" means reading.
#
# Typed rather than a free-form dict so a precondition check is a comparison
# and not a parse. A malformed dict would fail at the moment of checking, which
# is the moment before a write.


@dataclass(frozen=True, slots=True)
class CalendarRead:
    """A half-open [start, end) day range a call actually read.

    Half-open because that is what the calendar backend window is, and
    converting between inclusive and exclusive in two places is how an
    off-by-one day gets into a calendar write.
    """
    start: date
    end: date

    def covers(self, start: date, end: date) -> bool:
        return self.start <= start and self.end >= end


@dataclass(frozen=True, slots=True)
class CalendarWrite:
    """An event this turn put on the calendar, and the service confirmed.

    THE EXECUTOR SYNTHESISES THIS FROM THE SERVICE'S CONFIRMATION. A tool does
    not get to declare it, unlike a read. The reason is the asymmetry in what
    the two claims cost to be wrong about: a tool over-claiming a read makes a
    precondition pass that should not have, which the next read corrects. A
    tool over-claiming a write puts a line in the ledger saying an event exists
    when it does not — and the record is the thing a retry consults.

    So `committed` and this record are produced by the same code from the same
    WriteConfirmation, and cannot disagree.

    `identifier` is the service's own id. `day` is the day written to, which
    is what a later precondition asks about. `fingerprint` is the idempotency
    key (calendars/writes.py::write_fingerprint).
    """
    identifier: str
    day: date
    fingerprint: str
    verified: bool = False


@dataclass(frozen=True, slots=True)
class WriteAttempt:
    """A write that may or may not have landed.

    THIS IS WHY THE ToolError RULE HAD TO SPLIT. A failed read is not a read
    and records nothing — that rule is right and unchanged. A failed write is
    a different animal: a timeout or a dead socket means the request was
    already on its way out, and the service may have committed it.

    Under the old blanket rule this recorded nothing, so a retry would have had
    nothing to check against and would have had to ask the service — the exact
    service that just failed to answer. That is how a client-side timeout on a
    server-side success becomes two events on a calendar.

    It carries the fingerprint and not an identifier because there is no
    identifier: not knowing what happened is the whole content of this record.
    """
    fingerprint: str
    day: date
    detail: str = ""


# Every kind of fact a call can establish. A union rather than a base class
# with fields, so a precondition matching on kind is exhaustive and a new kind
# is a type error at every site that has to care.
Record = CalendarRead | CalendarWrite | WriteAttempt


@dataclass(frozen=True, slots=True)
class WriteConfirmation:
    """What a service said about a write, handed up for the executor to turn
    into a record and a `committed` flag.

    A write tool returns this instead of setting `committed` itself. That is
    the whole mechanism behind "committed and the write record cannot disagree
    because one produces the other" — there is exactly one function that reads
    this and it writes both.

    `status` mirrors calendars/writes.py::WriteStatus, restated here rather
    than imported so tools/types.py stays the tool layer's own vocabulary and
    does not depend on the calendar package. A second write target later (an
    email draft, a to-do) reports the same three states and needs no calendar.
    """
    status: Literal["written", "refused", "unknown"]
    day: date
    fingerprint: str
    identifier: str = ""
    verified: bool = False
    detail: str = ""


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
    # Set only by a write tool whose write failed. The executor reads it to
    # decide between "nothing happened, record nothing" (refused) and "this may
    # have landed, record an attempt" (unknown). None on every read tool, and
    # on a write that never got as far as attempting anything.
    write: WriteConfirmation | None = None

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

    `records` is what this call established as fact — see the Records note
    above. The executor writes it into the turn's ledger; the tool does not.
    A tool that establishes nothing returns none, and that is the honest
    answer. A WRITE TOOL LEAVES THIS EMPTY: the executor synthesises the
    record from `write` instead, and ignores anything declared here.

    `effects` is what should happen in the world as a result. The tool does
    not execute them — effects/runner.py does, in a fixed order. See
    tools/effects.py.

    `write` carries the service's answer for a write tool, and is None for a
    read. It is the executor's raw material for both the record and
    `committed`.

    `committed` records whether anything in the world actually changed. IT IS
    SET BY THE EXECUTOR, not by the tool: it may be true only when a service
    has confirmed the write back (invariant 4), and a tool that sets its own
    is a tool being trusted about the one thing worth checking. A value passed
    in here by a write tool is overwritten.
    """
    data: dict[str, Any] = field(default_factory=dict)
    records: tuple[Record, ...] = ()
    effects: tuple[Effect, ...] = ()
    write: WriteConfirmation | None = None
    committed: bool = False


# What any tool returns.
ToolOutcome = ToolResult | ToolError
