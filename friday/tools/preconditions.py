"""
tools/preconditions.py
What an update and a delete must have established before they may run.

    UPDATE_CALENDAR_EVENT = (CalendarReadFor(date_field="date"),)
    DELETE_CALENDAR_EVENT = (CalendarReadFor(date_field="date"),)
    target_proven(ledger, arguments, date_field) -> bool

NO UPDATE TOOL AND NO DELETE TOOL EXIST. This file is the requirement they
will be registered against, written and tested one step before they arrive.

WHY WRITE IT EARLY. The precondition machinery has been built since step 3 and
has never had a live consumer, because add_calendar_event correctly requires
no prior read — the user knows what they are asking for, and forcing a read
before every add costs a hop and a model call to establish something nobody
needed. So the one part of the write path that guards the dangerous
operations is the one part that has never been exercised against a real
requirement. The step that adds update and delete should be arguing about
update and delete, not discovering what a precondition means.

THE RULE, AND WHY IT IS THE DAY RATHER THAN THE EVENT.

An update or a delete names a TARGET, and the model can produce a plausible
identifier from its memory of the conversation exactly as easily as from a
read. "Move tennis to five" three turns after tennis was last mentioned is a
sentence about an event the model believes exists — and it may have been
deleted, moved, or never have been the event the user meant.

So the requirement is that the calendar for the target's day was read IN THIS
TURN. Not "a read happened" — tools/ledger.py is built around that
distinction — but that the specific day this call touches is inside recorded
coverage.

Day-level rather than event-level on purpose. An event-level precondition
would have to match identifiers, and the identifier is the thing in doubt: if
the model invented it, checking that it was "read" means checking the invented
value against a ledger that never saw it, which fails closed but tells the
model nothing useful. The day is a claim the model cannot fake and the read
that satisfies it returns the real identifiers as a side effect, which is what
actually fixes the problem.

PER-TURN, AND THAT IS THE WHOLE VALUE. A read from an earlier turn is not a
fact about now: the user may well have changed the calendar because of what
Friday just said. agent/turn.py creates the ledger and clears it in a finally
block for exactly this reason.

THE CONSTRAINT THIS PUTS ON THE FUTURE TOOL SIGNATURES — read this before
writing update_calendar_event.

CalendarReadFor resolves the target day out of the call's OWN ARGUMENTS. So an
update or delete tool MUST take the target's day as a parameter, even when it
also takes an event identifier and even though the identifier alone would be
enough for the service. A tool registered as

    delete_calendar_event(event_id: str)

cannot satisfy this precondition — there is no date field to check — and
tools/executor.py's ledger-less path fails closed, so it would simply never
run. That is the correct failure, but it is a confusing one to debug at the
moment you are writing the tool. Take the day.

The cost is one parameter the model has to get right; the benefit is that the
model must state which day it believes the event is on, which is a claim the
ledger can check. An identifier is not.

WHAT THIS FILE DOES NOT DO. It does not gate. policy/gating.py decides whether
an action needs a card, and it already answers correctly for both operations:
a delete is GATED whoever asked, and an update whose target was not proven by
a read this turn is GATED because the user stated the change but not WHICH
EVENT. target_proven() below is how a tool feeds the ledger's answer into that
decision — the gate deliberately does not read the ledger itself, so a gating
decision can be tested without constructing a turn.
"""

from __future__ import annotations

from datetime import date, timedelta

from tools.ledger import CalendarReadFor, Ledger

# The parameter an update or delete must carry. Named once so the tool
# registration, the precondition and target_proven() cannot drift apart.
TARGET_DAY_FIELD = "date"

# An update requires a read covering the target's day, in the same turn.
UPDATE_CALENDAR_EVENT: tuple = (CalendarReadFor(date_field=TARGET_DAY_FIELD),)

# A delete requires the same read. It is ALSO always gated, regardless of who
# asserted the fact — but that is policy/gating.py's answer, not a
# precondition, and the two are different questions:
#
#   the precondition asks  "has Friday looked?"      → refuses to run
#   the gate asks          "should the user decide?" → asks first
#
# A delete needs both. Having looked is not permission, and permission granted
# against an event nobody verified is permission to delete the wrong thing.
DELETE_CALENDAR_EVENT: tuple = (CalendarReadFor(date_field=TARGET_DAY_FIELD),)


def target_proven(ledger: Ledger, arguments: dict,
                  date_field: str = TARGET_DAY_FIELD) -> bool:
    """Whether the day this call targets was read in this turn.

    THE SAME QUESTION THE PRECONDITION ASKS, ANSWERED AS A BOOL. The
    precondition returns a sentence written to the model, because the model is
    what will go and satisfy it. policy/gating.py wants a boolean for
    Action.target_proven and has no use for prose.

    Two functions rather than one because their audiences differ and their
    failure modes differ: a precondition that returns None means "run", and a
    gate input that is False means "ask first" — inverted senses that would be
    a genuine hazard to conflate behind one return value.

    FAILS CLOSED. A missing or unparseable date answers False, which routes
    the action to GATED. An action whose target day cannot even be determined
    is not one to perform silently.
    """
    raw = arguments.get(date_field)
    if raw is None:
        return False
    try:
        day = date.fromisoformat(str(raw)[:10])
    except Exception:
        return False
    return ledger.covers_calendar(day, day + timedelta(days=1))
