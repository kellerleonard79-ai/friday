"""
policy/visibility.py
One question: may this item be shown on this surface at all?

    surfaces_in_briefing(priority) -> bool
    visible_on(surface, completed=...) -> bool

VISIBILITY IS NOT SUPPRESSION, AND KEEPING THEM APART IS THE POINT.

    Visibility   — may this EVER be shown here? A muted GroupMe group is
                   ingested for history and never surfaced, today or in an
                   hour. The answer does not depend on when you ask.
    Suppression  — should this be withheld RIGHT NOW because it was already
                   said? See policy/suppression.py. The answer is a function
                   of time and changes on its own.

Folded together they become one predicate that is sometimes false for two
unrelated reasons, and the first bug report is "why did it stop telling me
about X" with no way to tell which rule caught it.

This module makes a DECISION and performs no action — same contract as
policy/gating.py. It does not query, does not filter a result set, does not
touch the database. Callers use these values to build their queries and this
predicate to check an item they already hold.

WHY CRITERIA-AS-DATA RATHER THAN SQL. The rules below are enforced today by
SQL WHERE clauses, in two places that had drifted into being copies of each
other. Returning a SQL fragment from a policy module would put policy in the
business of acting; returning the VALUES lets each caller parameterize its own
query and lets a non-SQL consumer — the to-do list in step 8 — ask the same
question of an object it holds in memory.
"""

from __future__ import annotations

from typing import Literal

# Which GroupMe priority tiers a briefing may surface.
#
# The canonical tier list is connectors/groupme.py::PRIORITIES ("high",
# "normal", "muted"), and 'low' is its legacy spelling of 'muted'. This module
# does NOT import it — policy must not depend on connectors, and the direction
# of that dependency is worth more than the one-line convenience. What guards
# the drift instead is tests/test_policy.py, which asserts these are a subset
# of the connector's list, so renaming a tier fails a test rather than quietly
# turning a filter into a pass-through.
#
# 'muted' is deliberately absent: those rows are ingested so the history is
# complete and are never surfaced. That is the whole meaning of the tier.
BRIEFING_TIERS: tuple[str, ...] = ("high", "normal")

# Canvas urgencies a briefing carries. NORMAL items exist and are deliberately
# not here — a briefing that lists every assignment is a briefing nobody reads.
BRIEFING_URGENCIES: tuple[str, ...] = ("URGENT", "SOON")

# Where an item can be shown.
#
#   briefing          — the scheduled morning/evening message
#   telegram          — a chat reply or an interrupt
#   dashboard         — the dashboard's live surfaces
#   dashboard_history — the transcript and the Today feed. THE RECORD, not a
#                       surface competing for attention, which is why the
#                       completed rule treats it differently from every other.
Surface = Literal["briefing", "telegram", "dashboard", "dashboard_history"]

_RECORD_SURFACES: tuple[str, ...] = ("dashboard_history",)


def surfaces_in_briefing(priority: str | None) -> bool:
    """Whether a GroupMe row of this tier may appear in a briefing.

    Unknown and unset tiers answer False. connectors/groupme.py normalizes
    every configured value to a canonical tier before storing it, so anything
    unrecognised arriving here is a row written by something that predates
    that normalization — and the safe reading of "we do not know how loud this
    group is" is the quiet one.
    """
    return (priority or "").strip().lower() in BRIEFING_TIERS


def briefing_tier_tags() -> tuple[str, ...]:
    """The stored body markers for the surfacing tiers.

    connectors/groupme.py prepends "[priority=<tier>]" to every body it
    stores, and the briefing bundle matches on those markers because the tier
    is not its own column. Derived from BRIEFING_TIERS rather than spelled out
    again in a SQL string, which is where it lived until step 6 — with no link
    at all to the tier list it was supposed to be tracking.
    """
    return tuple(f"[priority={tier}]" for tier in BRIEFING_TIERS)


def visible_on(surface: Surface, *, completed: bool = False) -> bool:
    """Whether an item in this state may be shown on this surface.

    THE COMPLETED RULE: a completed item is invisible everywhere except the
    dashboard's history. Finishing something is the user telling Friday to
    stop bringing it up, and an assistant that keeps listing what you already
    did is one you stop reading. The record still has to exist — "did I
    already do that" is a real question — so history is the one surface that
    keeps showing it.

    NO CONSUMER YET. Nothing in Friday has a completable item: to-dos arrive
    in step 8, and that is what will call this. It is written now for the same
    reason the gating table's inferred row was written before any connector
    produced an inferred fact — the moment you are writing the to-do list is
    the wrong moment to also be deciding what "done" means to every surface.

    Deliberately NOT extended to channel selection. Choosing between Telegram
    and the dashboard needs presence, which is step 9, and a visibility rule
    that guessed at it would be a presence implementation hiding in the wrong
    module.
    """
    if completed:
        return surface in _RECORD_SURFACES
    return True
