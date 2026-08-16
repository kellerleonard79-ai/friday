"""
tools/calendar_write.py
The first write Friday can make to the real world, and the gate in front of it.

TWO TOOLS, AND THE SPLIT IS THE DESIGN.

    add_calendar_event      what the model calls. PROPOSES. Writes nothing,
                            emits a permission card, and stops.
    commit_calendar_event   what the CARD calls when the user taps Confirm.
                            Writes. Never offered to any model.

They are separate registrations rather than one tool with a "skip the gate"
parameter, because the registry derives every tool's schema from its signature:
a bypass parameter would be a bypass parameter THE MODEL CAN SEE AND SET. The
gate has to be something the model cannot address, and the cleanest way to do
that is to not give it a name the model is ever told.

commit_calendar_event's scope is ("internal",), which no profile's tool_scope
intersects, so it is never in a prompt. effects/pending.py reaches it by name
because it holds the name from the stored row, not from a model.

WHY THIS IS GATED WHEN THE POLICY TABLE SAYS AUTO. policy/gating.py routes a
user-stated create to AUTO, and typing "add lunch Thursday" is user-stated —
so by the table this tool would write with no card. It is wired to GATED
anyway, deliberately, for step 4. See the note at the wiring site below.

IDEMPOTENCY IS CHECKED IN THE COMMIT TOOL, not here. Nothing is written by a
proposal, so there is nothing to double-book; and the check has to happen as
close to the write as it can get.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from typing import Annotated

from actions import calendar as cal_action
from calendars import writes as cal_writes
from calendars.writes import write_fingerprint
from effects import pending
from memory import writes as recent_writes
from policy.gating import Action, needs_card
from tools import preconditions
from tools.registry import tool
from tools.types import (SendMessage, SendPermissionCard, ToolError,
                         ToolOutcome, ToolResult, WriteConfirmation)

logger = logging.getLogger("friday.tools.calwrite")

_config: dict = {}
_conn = None


def configure(config: dict, conn=None) -> None:
    """Install the config and the database handle. Called once at startup.

    A tool cannot be handed a connection per call — its arguments are the
    model's — so the handle lives here. It is the same connection the rest of
    the process uses; SQLite serializes writes and these are small.
    """
    global _config, _conn
    _config = config or {}
    _conn = conn


def _default_calendar() -> str:
    return ((_config.get("agent") or {}).get("default_calendar") or "").strip()


def _normalize(title: str, date: str, start_time: str, end_time: str,
               calendar: str) -> dict:
    """The event dict actions/calendar.py speaks, with the calendar resolved.

    Resolved HERE so the card names the calendar that will actually be written
    to. A card promising "Calendar: Work" that then falls back to the default
    has told the user something untrue at the exact moment they were deciding.
    """
    return {
        "title": (title or "").strip(),
        "date": (date or "").strip(),
        "start_time": (start_time or "").strip(),
        "end_time": (end_time or "").strip(),
        "calendar": (calendar or "").strip() or _default_calendar(),
    }


def _validate(event: dict) -> ToolError | None:
    if not event["title"]:
        return ToolError(kind="invalid_argument",
                         message="title is empty.", field="title")
    try:
        date_cls.fromisoformat(event["date"])
    except ValueError:
        return ToolError(
            kind="invalid_argument",
            message=f"date={event['date']!r} is not an ISO date (YYYY-MM-DD).",
            field="date")
    for field_name in ("start_time", "end_time"):
        value = event[field_name]
        if not value:
            continue
        try:
            hour, minute = (int(x) for x in value.split(":", 1))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            return ToolError(
                kind="invalid_argument",
                message=f"{field_name}={value!r} is not a 24-hour HH:MM time.",
                field=field_name)
    if event["end_time"] and not event["start_time"]:
        return ToolError(kind="invalid_argument",
                         message="end_time was given without start_time.",
                         field="start_time")
    return None


def _card_text(event: dict) -> str:
    """The card, as a TEMPLATE. Never model output.

    It states the action and its parameters literally, in the order a person
    checks them: what, when, where it lands. No persona voice (invariant 6),
    no quip, no preamble — the first line the user reads is the question they
    are being asked.

    Times are shown as the model supplied them, 24-hour, because a card is for
    verifying what will happen and a reformatting step is one more place the
    displayed value can drift from the stored one.
    """
    when = event["date"]
    if event["start_time"] and event["end_time"]:
        when += f" {event['start_time']}–{event['end_time']}"
    elif event["start_time"]:
        when += f" at {event['start_time']}"
    else:
        when += " (all day)"
    return (
        "Add to calendar?\n\n"
        f"Event: {event['title']}\n"
        f"When: {when}\n"
        f"Calendar: {event['calendar'] or 'default'}"
    )


@tool(
    name="add_calendar_event",
    description="Adds a new event to the user's calendar.",
    scope=("write",),
    # Proposes; does not write. See the module docstring.
    effect="gated_write",
    # NO PRECONDITIONS, deliberately. Creating an event does not require having
    # read the calendar first — the user knows what they are asking for, and
    # forcing a read before every add costs a hop and a model call to establish
    # something nobody needed. The precondition machinery is for updates and
    # deletes, which name an existing event and therefore have to have looked.
    preconditions=(),
    timeout_s=20.0,
)
def add_calendar_event(
    title: Annotated[str, "The event's name, in Title Case. Short and concrete — what it IS, not a sentence about it."],
    date: Annotated[str, "The day, ISO YYYY-MM-DD."],
    start_time: Annotated[str, "Start time, 24-hour local HH:MM. Leave empty for an all-day event."] = "",
    end_time: Annotated[str, "End time, 24-hour local HH:MM. Leave empty for a one-hour event."] = "",
    calendar: Annotated[str, "Calendar name. Leave empty for the user's default."] = "",
) -> ToolOutcome:
    """Call when the user asks for something to be added to their calendar."""
    event = _normalize(title, date, start_time, end_time, calendar)
    problem = _validate(event)
    if problem is not None:
        return problem

    # THE POLICY TABLE SAYS AUTO HERE, AND WE ARE OVERRIDING IT — DELIBERATELY.
    #
    # Everything reaching this tool today is user-stated: the only input
    # channel is the user typing, so policy/gating.py routes it to AUTO and a
    # correct implementation of the table would write with no card at all.
    #
    # It is forced to GATED for step 4 on purpose. The card carries the
    # invariant this step exists to make structural — emitted first, ahead of
    # everything, by an ordering guarantee rather than by a rule people
    # remember — and the first write Friday makes to a real calendar should not
    # go out unreviewed. The AUTO path gets exercised in the step that adds
    # update and delete, where the table's other cells have live producers too.
    #
    # The decision is still computed rather than assumed, so this reads as an
    # override rather than as the table being wrong.
    decision_would_be = needs_card(
        Action(operation="create", provenance="user_stated",
               tool="add_calendar_event"))
    if not decision_would_be:
        logger.info(
            "add_calendar_event: policy says AUTO for a user-stated create; "
            "gating anyway for step 4.")

    key = pending.new_key()
    proposal = _card_text(event)
    if not pending.stage(
        _conn, key=key, tool="commit_calendar_event",
        arguments=event, proposal=proposal,
        ttl_min=pending.ttl_minutes(_config),
    ):
        # Staged BEFORE the card is sent, so a failure here means no card goes
        # out at all. A card whose row does not exist is a button that answers
        # "I can't find that request" the instant it is tapped.
        return ToolError(
            kind="unavailable",
            message="The approval could not be recorded, so no card was sent.")

    return ToolResult(
        data={"status": "awaiting_confirmation", "proposal": event},
        effects=(SendPermissionCard(
            proposal=proposal, pending_key=key,
            tool="commit_calendar_event", arguments=event),),
    )


@tool(
    name="commit_calendar_event",
    description="Internal. Performs a calendar write the user has already approved.",
    # No profile's tool_scope intersects this, so it is never in a prompt.
    # effects/pending.py reaches it by name, from the stored row.
    scope=("internal",),
    effect="write",
    timeout_s=60.0,
)
def commit_calendar_event(
    title: Annotated[str, "The event's name."],
    date: Annotated[str, "The day, ISO YYYY-MM-DD."],
    start_time: Annotated[str, "Start time, 24-hour HH:MM."] = "",
    end_time: Annotated[str, "End time, 24-hour HH:MM."] = "",
    calendar: Annotated[str, "Calendar name."] = "",
) -> ToolOutcome:
    """Never call this. It runs only from a confirmed permission card."""
    event = _normalize(title, date, start_time, end_time, calendar)
    problem = _validate(event)
    if problem is not None:
        return problem

    day = date_cls.fromisoformat(event["date"])
    fingerprint = write_fingerprint(
        event["title"], event["date"], event["start_time"], event["calendar"])

    # CHECKED BEFORE THE WRITE, AND THEREFORE BEFORE ANY RETRY — a retry
    # reaches this same line. The check is local because the case it exists for
    # is a write whose service call timed out, and asking the service again is
    # asking the thing that just failed to answer.
    prior = recent_writes.find(_conn, fingerprint)
    if prior is not None:
        logger.warning(
            f"commit_calendar_event: {event['title']!r} on {event['date']} "
            f"matches a write from {prior.created_at} ({prior.status}) — "
            f"not writing again.")
        return ToolResult(
            data={"status": "already_written", "identifier": prior.identifier},
            effects=(SendMessage(
                text=f"{event['title']} is already on your calendar for "
                     f"{event['date']}, sir — I didn't add it twice.",
                quip_key="commit_calendar_event:no_op"),),
            # Reported as written, because it is: the event exists. `verified`
            # carries whether the earlier write was ever confirmed, which is
            # the honest answer for a prior attempt that timed out.
            write=WriteConfirmation(
                status="written", day=day, fingerprint=fingerprint,
                identifier=prior.identifier, verified=prior.confirmed,
                detail="suppressed by recent_writes"),
        )

    recent_writes.reserve(_conn, fingerprint, detail=event["title"])
    outcome = cal_action.write_outcome(event, default_calendar=_default_calendar())
    recent_writes.settle(_conn, fingerprint, status=outcome.status,
                         identifier=outcome.uid, detail=outcome.detail)

    confirmation = WriteConfirmation(
        status=outcome.status, day=day, fingerprint=fingerprint,
        identifier=outcome.uid, verified=outcome.verified,
        detail=outcome.detail)

    if outcome.status == "written":
        # THE TOOL DOES NOT CHOOSE THE QUIP. It names the outcome; the effects
        # runner renders it. A tool that appended personality would be a tool
        # deciding how its answer reads, which is the thing tools/types.py
        # exists to prevent — and a quip key on SendMessage but not on
        # SendPermissionCard is what makes "never a quip on a card" structural
        # rather than a rule.
        return ToolResult(data={"status": "written", "identifier": outcome.uid,
                                "verified": outcome.verified},
                          effects=(SendMessage(
                              text=cal_action.format_confirmation(event),
                              quip_key="commit_calendar_event:success"),),
                          write=confirmation)

    # Refused and unknown read the same to the user. The difference matters to
    # the ledger and to a retry, not to a sentence in a chat window.
    logger.error(f"commit_calendar_event {outcome.status}: {outcome.detail}")
    return ToolError(
        kind="unavailable",
        message=f"The calendar write {outcome.status}: {outcome.detail}",
        write=confirmation,
    )


# ── Update and delete ─────────────────────────────────────────────────────────
#
# SAME SPLIT AS ADD: a propose tool that writes nothing and a commit tool the
# card calls, never offered to any model. Everything about the split's reasons
# above applies unchanged.
#
# THE PROPOSE AND COMMIT TOOLS SHARE ONE SIGNATURE, deliberately — the same
# choice add_calendar_event and commit_calendar_event already made. The
# arguments stored on the card are exactly what the propose tool validated,
# and the commit path replays them into a tool with the identical parameter
# set rather than re-deriving anything (effects/pending.py's whole point).
#
# `title` IS CARRIED ON BOTH EVEN THOUGH NEITHER WRITE TOUCHES IT — it is the
# event's CURRENT name, supplied by the model from a get_schedule read, and it
# exists so the card and the confirmation can name what is being changed or
# removed without a second read. A card for a delete that cannot say what it
# is deleting is not a card anyone can meaningfully approve.
#
# PRECONDITIONS SIT ONLY ON THE PROPOSE TOOL. The commit tool runs from
# effects/pending.py::confirm() against a fresh, empty per-call ledger — the
# turn that justified the proposal is over. A precondition on the commit tool
# would therefore fail closed on every single confirmation, unconditionally.
# tools/preconditions.py's UPDATE_CALENDAR_EVENT / DELETE_CALENDAR_EVENT exist
# for exactly the propose tool's registration and nowhere else.


def _validate_update(uid: str, date: str, title: str, new_title: str,
                     new_date: str, new_start_time: str,
                     new_end_time: str) -> ToolError | None:
    if not uid:
        return ToolError(kind="invalid_argument", message="uid is empty.", field="uid")
    try:
        date_cls.fromisoformat(date)
    except ValueError:
        return ToolError(
            kind="invalid_argument",
            message=f"date={date!r} is not an ISO date (YYYY-MM-DD).",
            field="date")
    if not title:
        return ToolError(kind="invalid_argument", message="title is empty.", field="title")
    if new_date:
        try:
            date_cls.fromisoformat(new_date)
        except ValueError:
            return ToolError(
                kind="invalid_argument",
                message=f"new_date={new_date!r} is not an ISO date (YYYY-MM-DD).",
                field="new_date")
    for field_name, value in (("new_start_time", new_start_time),
                              ("new_end_time", new_end_time)):
        if not value:
            continue
        try:
            hour, minute = (int(x) for x in value.split(":", 1))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            return ToolError(
                kind="invalid_argument",
                message=f"{field_name}={value!r} is not a 24-hour HH:MM time.",
                field=field_name)
    if not any((new_title, new_date, new_start_time, new_end_time)):
        return ToolError(
            kind="invalid_argument",
            message=(
                "Nothing to change — supply at least one of new_title, "
                "new_date, new_start_time, new_end_time."
            ),
            field="new_title")
    return None


def _update_card_text(title: str, new_title: str, new_date: str,
                      new_start_time: str, new_end_time: str) -> str:
    """The card, as a TEMPLATE — see _card_text above for why."""
    lines = ["Update this event?", "", f"Event: {title}"]
    if new_title:
        lines.append(f"New title: {new_title}")
    if new_date or new_start_time or new_end_time:
        when = new_date or "(same day)"
        if new_start_time and new_end_time:
            when += f" {new_start_time}–{new_end_time}"
        elif new_start_time:
            when += f" at {new_start_time}"
        lines.append(f"New time: {when}")
    return "\n".join(lines)


@tool(
    name="update_calendar_event",
    description="Proposes a change to an existing calendar event.",
    scope=("write",),
    # Proposes; does not write. See the module note above.
    effect="gated_write",
    preconditions=preconditions.UPDATE_CALENDAR_EVENT,
    timeout_s=20.0,
)
def update_calendar_event(
    uid: Annotated[str, "The event's identifier, from a get_schedule result covering its day."],
    date: Annotated[str, "The day the event is on now, ISO YYYY-MM-DD. get_schedule must have read this day already this turn."],
    title: Annotated[str, "The event's current title, as returned by get_schedule."],
    calendar: Annotated[str, "The event's calendar, from get_schedule. Narrows the search."] = "",
    new_title: Annotated[str, "New title. Leave empty to keep the current one."] = "",
    new_date: Annotated[str, "New day, ISO YYYY-MM-DD, if moving the event. Leave empty to keep the current day."] = "",
    new_start_time: Annotated[str, "New start time, 24-hour HH:MM. Leave empty to keep unchanged."] = "",
    new_end_time: Annotated[str, "New end time, 24-hour HH:MM. Leave empty to keep unchanged."] = "",
) -> ToolOutcome:
    """Call to change an event already on the calendar. Read its day with get_schedule first."""
    uid, date, title, calendar = uid.strip(), date.strip(), title.strip(), calendar.strip()
    new_title, new_date = new_title.strip(), new_date.strip()
    new_start_time, new_end_time = new_start_time.strip(), new_end_time.strip()

    problem = _validate_update(uid, date, title, new_title, new_date,
                               new_start_time, new_end_time)
    if problem is not None:
        return problem

    # By the time this line runs, tools/executor.py has already checked
    # preconditions.UPDATE_CALENDAR_EVENT against this turn's ledger, so
    # target_proven is always True here — an update whose target was NOT
    # proven never reaches this far; the precondition failure returns first.
    # policy/gating.py's table therefore says AUTO for this call. Computed
    # anyway and overridden GATED regardless, matching add_calendar_event's
    # own step-4 override: the first update Friday performs should not go out
    # unreviewed either. AUTO gets exercised the day something removes this
    # override, not before.
    decision_would_be = needs_card(Action(
        operation="update", provenance="user_stated",
        tool="update_calendar_event", target_proven=True))
    if not decision_would_be:
        logger.info(
            "update_calendar_event: policy says AUTO once the target is "
            "proven; gating anyway, matching add_calendar_event.")

    arguments = {
        "uid": uid, "date": date, "title": title, "calendar": calendar,
        "new_title": new_title, "new_date": new_date,
        "new_start_time": new_start_time, "new_end_time": new_end_time,
    }
    key = pending.new_key()
    proposal = _update_card_text(title, new_title, new_date, new_start_time, new_end_time)
    if not pending.stage(
        _conn, key=key, tool="commit_update_calendar_event",
        arguments=arguments, proposal=proposal,
        ttl_min=pending.ttl_minutes(_config),
    ):
        return ToolError(
            kind="unavailable",
            message="The approval could not be recorded, so no card was sent.")

    return ToolResult(
        data={"status": "awaiting_confirmation", "proposal": arguments},
        effects=(SendPermissionCard(
            proposal=proposal, pending_key=key,
            tool="commit_update_calendar_event", arguments=arguments),),
    )


@tool(
    name="commit_update_calendar_event",
    description="Internal. Performs a calendar update the user has already approved.",
    # No profile's tool_scope intersects this, so it is never in a prompt.
    scope=("internal",),
    effect="write",
    timeout_s=60.0,
)
def commit_update_calendar_event(
    uid: Annotated[str, "The event's identifier."],
    date: Annotated[str, "The day the event was on."],
    title: Annotated[str, "The event's title before this change."],
    calendar: Annotated[str, "The event's calendar."] = "",
    new_title: Annotated[str, "New title."] = "",
    new_date: Annotated[str, "New day, ISO YYYY-MM-DD."] = "",
    new_start_time: Annotated[str, "New start time, 24-hour HH:MM."] = "",
    new_end_time: Annotated[str, "New end time, 24-hour HH:MM."] = "",
) -> ToolOutcome:
    """Never call this. It runs only from a confirmed permission card."""
    uid, date, title, calendar = uid.strip(), date.strip(), title.strip(), calendar.strip()
    new_title, new_date = new_title.strip(), new_date.strip()
    new_start_time, new_end_time = new_start_time.strip(), new_end_time.strip()

    problem = _validate_update(uid, date, title, new_title, new_date,
                               new_start_time, new_end_time)
    if problem is not None:
        return problem

    retimed = bool(new_date or new_start_time or new_end_time)
    effective_date = new_date or date
    day = date_cls.fromisoformat(effective_date)
    fingerprint = cal_writes.target_fingerprint(
        "update", uid, new_title, new_date, new_start_time, new_end_time)

    # CHECKED BEFORE THE WRITE, AND THEREFORE BEFORE ANY RETRY — same
    # reasoning as commit_calendar_event's identical check above.
    prior = recent_writes.find(_conn, fingerprint)
    if prior is not None:
        logger.warning(
            f"commit_update_calendar_event: {uid} matches an update from "
            f"{prior.created_at} ({prior.status}) — not repeating it.")
        return ToolResult(
            data={"status": "already_updated", "identifier": prior.identifier},
            effects=(SendMessage(
                text=f"{title} is already updated, sir — I didn't do it twice.",
                quip_key="commit_update_calendar_event:no_op"),),
            write=WriteConfirmation(
                status="written", day=day, fingerprint=fingerprint,
                identifier=prior.identifier or uid, verified=prior.confirmed,
                detail="suppressed by recent_writes"),
        )

    recent_writes.reserve(_conn, fingerprint, detail=f"update:{title}")
    outcome = cal_action.update_outcome(
        uid, calendar=calendar,
        title=new_title or None,
        date=effective_date if retimed else None,
        start_time=new_start_time or None,
        end_time=new_end_time or None,
    )
    recent_writes.settle(_conn, fingerprint, status=outcome.status,
                         identifier=outcome.uid or uid, detail=outcome.detail)

    confirmation = WriteConfirmation(
        status=outcome.status, day=day, fingerprint=fingerprint,
        identifier=outcome.uid or uid, verified=outcome.verified,
        detail=outcome.detail)

    if outcome.status == "written":
        parts = []
        if new_title:
            parts.append("renamed")
        if retimed:
            when = f"moved to {effective_date}"
            if new_start_time:
                when += f" at {new_start_time}"
            parts.append(when)
        text = f"{new_title or title} updated"
        text += f" — {', '.join(parts)}." if parts else "."
        return ToolResult(
            data={"status": "written", "identifier": outcome.uid, "verified": outcome.verified},
            effects=(SendMessage(text=text,
                                 quip_key="commit_update_calendar_event:success"),),
            write=confirmation,
        )

    logger.error(f"commit_update_calendar_event {outcome.status}: {outcome.detail}")
    return ToolError(
        kind="unavailable",
        message=f"The calendar update {outcome.status}: {outcome.detail}",
        write=confirmation,
    )


def _validate_delete(uid: str, date: str, title: str) -> ToolError | None:
    if not uid:
        return ToolError(kind="invalid_argument", message="uid is empty.", field="uid")
    try:
        date_cls.fromisoformat(date)
    except ValueError:
        return ToolError(
            kind="invalid_argument",
            message=f"date={date!r} is not an ISO date (YYYY-MM-DD).",
            field="date")
    if not title:
        return ToolError(kind="invalid_argument", message="title is empty.", field="title")
    return None


def _delete_card_text(title: str, date: str, calendar: str) -> str:
    return (
        "Delete this event?\n\n"
        f"Event: {title}\n"
        f"Date: {date}\n"
        f"Calendar: {calendar or 'default'}\n\n"
        "This cannot be undone."
    )


@tool(
    name="delete_calendar_event",
    description="Proposes removing an existing event from the calendar.",
    scope=("write",),
    effect="gated_write",
    preconditions=preconditions.DELETE_CALENDAR_EVENT,
    timeout_s=20.0,
)
def delete_calendar_event(
    uid: Annotated[str, "The event's identifier, from a get_schedule result covering its day."],
    date: Annotated[str, "The day the event is on, ISO YYYY-MM-DD. get_schedule must have read this day already this turn."],
    title: Annotated[str, "The event's title, as returned by get_schedule. Shown on the confirmation card."],
    calendar: Annotated[str, "The event's calendar, from get_schedule. Narrows the search."] = "",
) -> ToolOutcome:
    """Call to remove an event already on the calendar. Read its day with get_schedule first."""
    uid, date, title, calendar = uid.strip(), date.strip(), title.strip(), calendar.strip()
    problem = _validate_delete(uid, date, title)
    if problem is not None:
        return problem

    # IRREVERSIBLE — GATED WHOEVER ASKED. policy/gating.py's table has no AUTO
    # cell for delete at all, so unlike update above this is not an override:
    # it is the table's own unconditional answer. Computed rather than
    # hardcoded so a future change to the table cannot silently stop being
    # honoured here.
    if not needs_card(Action(operation="delete", provenance="user_stated",
                             tool="delete_calendar_event")):
        logger.error(
            "delete_calendar_event: policy/gating.py returned AUTO for a "
            "delete, which the table should never do. Gating anyway.")

    arguments = {"uid": uid, "date": date, "title": title, "calendar": calendar}
    key = pending.new_key()
    proposal = _delete_card_text(title, date, calendar)
    if not pending.stage(
        _conn, key=key, tool="commit_delete_calendar_event",
        arguments=arguments, proposal=proposal,
        ttl_min=pending.ttl_minutes(_config),
    ):
        return ToolError(
            kind="unavailable",
            message="The approval could not be recorded, so no card was sent.")

    return ToolResult(
        data={"status": "awaiting_confirmation", "proposal": arguments},
        effects=(SendPermissionCard(
            proposal=proposal, pending_key=key,
            tool="commit_delete_calendar_event", arguments=arguments),),
    )


@tool(
    name="commit_delete_calendar_event",
    description="Internal. Performs a calendar deletion the user has already approved.",
    scope=("internal",),
    effect="write",
    timeout_s=60.0,
)
def commit_delete_calendar_event(
    uid: Annotated[str, "The event's identifier."],
    date: Annotated[str, "The day the event was on."],
    title: Annotated[str, "The event's title."],
    calendar: Annotated[str, "The event's calendar."] = "",
) -> ToolOutcome:
    """Never call this. It runs only from a confirmed permission card."""
    uid, date, title, calendar = uid.strip(), date.strip(), title.strip(), calendar.strip()
    problem = _validate_delete(uid, date, title)
    if problem is not None:
        return problem

    day = date_cls.fromisoformat(date)
    fingerprint = cal_writes.target_fingerprint("delete", uid)

    prior = recent_writes.find(_conn, fingerprint)
    if prior is not None:
        logger.warning(
            f"commit_delete_calendar_event: {uid} matches a delete from "
            f"{prior.created_at} ({prior.status}) — not repeating it.")
        return ToolResult(
            data={"status": "already_deleted", "identifier": prior.identifier},
            effects=(SendMessage(
                text=f"{title} is already gone, sir — I didn't try again.",
                quip_key="commit_delete_calendar_event:no_op"),),
            write=WriteConfirmation(
                status="written", day=day, fingerprint=fingerprint,
                identifier=prior.identifier or uid, verified=prior.confirmed,
                detail="suppressed by recent_writes"),
        )

    recent_writes.reserve(_conn, fingerprint, detail=f"delete:{title}")
    outcome = cal_action.delete_outcome(uid, calendar=calendar)
    recent_writes.settle(_conn, fingerprint, status=outcome.status,
                         identifier=outcome.uid or uid, detail=outcome.detail)

    confirmation = WriteConfirmation(
        status=outcome.status, day=day, fingerprint=fingerprint,
        identifier=outcome.uid or uid, verified=outcome.verified,
        detail=outcome.detail)

    if outcome.status == "written":
        return ToolResult(
            data={"status": "written", "identifier": outcome.uid, "verified": outcome.verified},
            effects=(SendMessage(text=f"{title} removed, sir.",
                                 quip_key="commit_delete_calendar_event:success"),),
            write=confirmation,
        )

    logger.error(f"commit_delete_calendar_event {outcome.status}: {outcome.detail}")
    return ToolError(
        kind="unavailable",
        message=f"The calendar deletion {outcome.status}: {outcome.detail}",
        write=confirmation,
    )
