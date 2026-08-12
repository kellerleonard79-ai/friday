"""
dashboard/stream.py
Server→client push, in about a hundred lines of which twenty are code.

WHY SSE AND NOT WEBSOCKET. Only one direction is needed: the browser already
has POST for everything it wants to say, and the server needs a way to speak
without being asked — a reply arriving, a card appearing, a card resolved from
Telegram. SSE is a long-lived GET with a text/event-stream body, it reconnects
by itself when the Mac sleeps or the Wi-Fi drops, and it needs no library on
either end. A WebSocket would be a second protocol, a second failure mode, and
a dependency, in exchange for an upstream channel that already exists.

THREAD SAFETY IS THE WHOLE PROBLEM HERE. Effects run in an executor thread —
they are blocking sends, and channels/conversation.py deliberately keeps them
off the event loop. So publish() is called from a thread that does not own the
loop, and touching an asyncio.Queue from there is a race with no error
message. Everything crosses back over via call_soon_threadsafe, which is the
one documented way in.

A SUBSCRIBER THAT CANNOT KEEP UP IS DROPPED, NOT WAITED FOR. Queues are
bounded. A browser that has been suspended in a background tab for an hour
must not become a memory leak, and it must never become backpressure on a
turn — an effect that blocked because nobody was reading a stream would be a
message the user did not get, in order to deliver a message to a browser that
is not there.

THE STREAM IS NOT THE RECORD. Every event delivered here is also either in
conversation_history or in pending_actions, and every route that produces one
also returns it in its own response. A browser that missed the stream — first
load, a reconnect, a tab opened mid-turn — recovers by reading, not by
replaying. That is why there is no event id and no Last-Event-ID handling: a
replay buffer would be a second, worse copy of the database.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("friday.dashboard.stream")

# Per-subscriber backlog. Small on purpose — see the module docstring. A turn
# produces a handful of events, so anything over this means nobody is reading.
_QUEUE_MAX = 64

# How often to write a comment line when nothing is happening. Proxies and
# laptops close idle connections; a colon-prefixed line is a no-op to the
# EventSource API and keeps the socket honest.
_HEARTBEAT_S = 20.0


class Broadcaster:
    """Fan one event out to every attached browser."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the loop publish() has to hand work back to.

        Called from subscribe(), not from a startup hook. That is not laziness
        for its own sake: publish() is a no-op with no subscribers, so the
        loop cannot be needed before the first one attaches, and binding at
        the moment of attachment means there is no ordering to get wrong and
        no deprecated startup event to maintain.
        """
        self._loop = loop

    # ── Publishing (called from ANY thread) ──────────────────────────────────

    def publish(self, event: dict) -> None:
        """Queue an event for every subscriber. Never raises, never blocks.

        Safe from an executor thread, which is where every effect runs.
        """
        loop = self._loop
        if loop is None or not self._subscribers:
            return
        try:
            loop.call_soon_threadsafe(self._publish_on_loop, event)
        except RuntimeError:
            # The loop is closing. Nothing to deliver to, and a shutting-down
            # dashboard must not take a message down with it.
            logger.debug("stream publish after loop close — dropped")

    def _publish_on_loop(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Dropped rather than awaited. See the module docstring: a
                # suspended tab must never become backpressure on a turn.
                logger.debug("stream subscriber is not keeping up — event dropped")

    # ── Subscribing (called from the loop) ───────────────────────────────────

    async def subscribe(self):
        """Yield SSE frames until the client goes away.

        The finally is load-bearing: a browser that navigates away cancels this
        generator, and a subscriber left in the set is an unbounded queue
        nobody will ever read.
        """
        self.bind(asyncio.get_running_loop())
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subscribers.add(queue)
        try:
            # Told immediately rather than after the first event, so a browser
            # can show "connected" without waiting for something to happen.
            yield _frame({"kind": "hello"})
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(),
                                                   timeout=_HEARTBEAT_S)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _frame(event)
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


def _frame(event: dict) -> str:
    """One SSE frame.

    The payload is JSON on a single line because the wire format is
    newline-delimited: a raw newline inside `data:` starts a new field and
    would split one message into two. json.dumps escapes them, which is the
    whole reason the body is JSON rather than the text itself — Friday's
    replies contain newlines routinely.
    """
    return f"data: {json.dumps(event, default=str)}\n\n"
