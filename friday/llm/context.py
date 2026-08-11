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
"""

from __future__ import annotations

import logging

import clock
from llm.types import ContextBlock

logger = logging.getLogger("friday.llm.context")


def time_block(config: dict) -> ContextBlock:
    """Wall clock, spelled out. Never omitted — this one cannot fail.

    The weekday is written out in full and separately from the date because
    the persona's TIME section tells the model to use it verbatim rather than
    derive it. Giving only the ISO date would leave it deriving one anyway.
    """
    now = clock.local_now(config)
    lines = [
        f"Current date and time: {clock.human(now)}",
        f"Day of week: {now.strftime('%A')}",
        f"Today's date (ISO): {now.date().isoformat()}",
        f"Current time (ISO): {now.isoformat(timespec='seconds')}",
        f"Timezone: {clock.timezone_name(config)}",
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
