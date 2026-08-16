"""
tools/work_write.py
The chat path onto the Work list: "add 'clean the car' to my to-do list."

ONE TOOL, NO CARD — unlike tools/calendar_write.py. policy/gating.py's table
already says AUTO for a user-stated create, and add_calendar_event overrides
that deliberately, for reasons specific to a calendar write (a double-booking
is a real-world collision with someone else's time, or Friday's own advice
about the day). A personal to-do has no such hazard: worst case, an unwanted
row sits in a to-do list until dismissed. So this tool is not overridden — it
IS the table's AUTO cell, exercised for the first time by something other than
Canvas due-date sync.

Writes straight to memory/work.py, not through the calendar backend — a task
has no calendar to land on and no external service to confirm it back, so
there is no `unknown` state to protect against and no idempotency fingerprint
worth checking (a double "add clean the car" is just a second real chore).
"""

from __future__ import annotations

import logging
from datetime import date as date_cls
from typing import Annotated

from memory import work
from tools.registry import tool
from tools.types import SendMessage, ToolError, ToolOutcome, ToolResult, WriteConfirmation

logger = logging.getLogger("friday.tools.work")

_conn = None


def configure(conn=None) -> None:
    """Install the database handle. Called once at startup, same pattern as
    tools/calendar_write.py::configure."""
    global _conn
    _conn = conn


@tool(
    name="add_task",
    description="Adds a personal task to Keller's to-do list.",
    scope=("write",),
    effect="write",
    preconditions=(),
    timeout_s=10.0,
)
def add_task(
    title: Annotated[str, "The task, short and concrete — what needs doing."],
    due_date: Annotated[str, "ISO YYYY-MM-DD, if there's a deadline. Leave empty for a task with no due date."] = "",
    estimated_minutes: Annotated[int, "How long it'll take, in minutes, if known. Leave 0 if unknown."] = 0,
) -> ToolOutcome:
    """Call when the user asks to add something to their to-do list or track a personal task."""
    title = (title or "").strip()
    if not title:
        return ToolError(kind="invalid_argument", message="title is empty.", field="title")

    due_date = (due_date or "").strip()
    if due_date:
        try:
            date_cls.fromisoformat(due_date)
        except ValueError:
            return ToolError(
                kind="invalid_argument",
                message=f"due_date={due_date!r} is not an ISO date (YYYY-MM-DD).",
                field="due_date")

    row = work.add_manual(
        _conn, title=title, due_at=due_date or None,
        has_due_time=False,  # a chat-supplied date is a day, never a time
        estimated_minutes=estimated_minutes or None,
    )

    text = f'Added "{title}" to your to-do list'
    text += f", due {due_date}." if due_date else "."

    return ToolResult(
        data={"status": "added", "id": row["id"]},
        effects=(SendMessage(text=text, quip_key="add_task:success"),),
        # Reused from the calendar vocabulary — WriteConfirmation's own
        # docstring names "a to-do" as exactly the second write target it was
        # generalised for. `day` takes the due date when there is one and
        # today otherwise; nothing downstream reads it as a calendar day, only
        # tools/executor.py's ledger bookkeeping does, and add_task declares
        # no preconditions for anything to check it against.
        write=WriteConfirmation(
            status="written",
            day=date_cls.fromisoformat(due_date) if due_date else date_cls.today(),
            fingerprint=f"task-{row['id']}",
            identifier=str(row["id"]),
            verified=True,
        ),
    )
