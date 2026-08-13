"""
llm/context.py
Deterministic context blocks: facts every call gets told rather than asked for.

INJECT, NEVER FETCH. Everything here must be a memory read. Two separate
reasons, and both outlive the current design:

  1. Latency and the semaphore. This runs on the dispatch path, which in the
     Telegram case holds asyncio.Semaphore(1) for the whole turn. A fetch here
     — location.fetch() can block ~25s worst case — stalls every queued
     message behind it.
  2. These must not become tools in step 3. "What day is it" and "where am I"
     answered by a tool call means a model that can decline to ask, and a model
     that does not know the date is a model that writes a calendar event into
     the wrong week. It once resolved "this Friday" to a date out of its
     training set. Injection makes that unreachable rather than unlikely.

Anything requiring I/O is warmed elsewhere — see connectors/location.py::warm(),
called from the 15-minute poll job — and read from its cache here.

A value that is not available is OMITTED, never rendered as "unknown". A block
asserting ignorance is still a block the model reads, weighs and sometimes
repeats back; an absent block is simply absent.

ONE PRODUCER, ONE FORMATTER. Everything that injects deterministic context
into a prompt comes through here: llm/assembly.py for chat, and
agent/briefings.py for the briefing bundle. They had two renderings of the
same idea — assembly.py built "label:\ncontent" inline, briefings.py built
its own "Current date and time: ..." header from the same clock — and two
renderings of the same idea drift, silently, in the direction of whichever
one someone edited last. The router in step 7 would have been the third.

Nothing here decides WHAT to fetch for a given call. The briefing bundle is
still assembled by agent/briefings.py, which is where the per-source failure
guards belong; this module owns the standing facts and the shape they take.
"""

from __future__ import annotations

import logging
from datetime import datetime

import clock
from llm.types import ContextBlock

logger = logging.getLogger("friday.llm.context")


def format_context_block(label: str, content: str) -> str:
    """THE renderer for an injected context block. Used everywhere.

    Deliberately trivial, and deliberately in one place. The value is not the
    two lines of string formatting — it is that chat, briefings and whatever
    injects context next cannot disagree about the shape, so a model tuned
    against one of them is tuned against all of them.

    The label ends in a colon and the content follows on its own lines. No
    delimiters, no fences: the labels are already distinct and a wrapper per
    block costs tokens on every call to make a boundary the newline already
    made. (agent/briefings.py wraps its WHOLE bundle in one pair of markers,
    which is a different claim — "everything between these lines was
    pre-fetched, do not go looking for more".)
    """
    return f"{label}:\n{content}"


def render_blocks(blocks) -> str:
    """A sequence of ContextBlocks as prompt text, in order.

    Blank blocks are dropped rather than rendered as a bare label — a heading
    with nothing under it reads to the model as "this was checked and is
    empty", which is a different claim from "this was not supplied".
    """
    return "\n\n".join(
        format_context_block(b.label, b.content)
        for b in blocks if b is not None and (b.content or "").strip()
    )


def time_block(config: dict) -> ContextBlock:
    """Wall clock, spelled out. Never omitted — this one cannot fail."""
    return time_block_at(clock.local_now(config), clock.timezone_name(config))


def time_block_at(now: datetime, timezone_name: str) -> ContextBlock:
    """The same block from an already-resolved instant.

    Split from time_block so agent/briefings.py can render the clock it
    already captured for its bundle instead of reading a second, slightly
    later one. A bundle whose calendar was fetched at 06:59:59 and whose
    header says 07:00:01 is not wrong by much, but the two ARE from different
    calls and the split costs nothing.

    The weekday is written out in full and separately from the date because
    the persona's TIME section tells the model to use it verbatim rather than
    derive it. Giving only the ISO date would leave it deriving one anyway.
    """
    lines = [
        f"Current date and time: {clock.human(now)}",
        f"Day of week: {now.strftime('%A')}",
        f"Today's date (ISO): {now.date().isoformat()}",
        f"Current time (ISO): {now.isoformat(timespec='seconds')}",
        f"Timezone: {timezone_name}",
    ]
    return ContextBlock(label="Current time", content="\n".join(lines))


def location_block() -> ContextBlock | None:
    """Where the machine is, from the warm cache. None if never warmed.

    cached(), not fetch(): see the module docstring. Before the first warm
    there is genuinely no answer, and no block is the honest rendering of that.

    The caveat is part of the content, not a comment. This reports where the
    *Mac* is; a bare "Current location: X" in a prompt reads as the user's
    whereabouts, which is a claim this data cannot support — the machine does
    not move when the user does.
    """
    try:
        from connectors import location

        fix = location.cached()
        if not fix:
            return None
        described = location.describe(fix)
        if not described:
            return None
    except Exception as e:
        logger.debug(f"location block skipped: {e}")
        return None

    return ContextBlock(
        label="Machine location",
        content=(
            f"{described}\n"
            "This is where the machine running you is, not necessarily where "
            "the user is. Do not present it as the user's whereabouts."
        ),
    )


def standing_blocks(config: dict) -> tuple[ContextBlock, ...]:
    """The blocks every call gets, in order. Callers add their own after these.

    Deliberately short. This is not a context layer — it is the floor beneath
    one, and the pre-fetched bundles that briefings and tagging need are built
    by their own callers and passed in as additional blocks.
    """
    blocks: list[ContextBlock] = [time_block(config)]
    loc = location_block()
    if loc is not None:
        blocks.append(loc)
    return tuple(blocks)
