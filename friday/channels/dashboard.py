"""
channels/dashboard.py
The dashboard, as a channel.

WHY IT EXISTS. Telegram is blocked on school Wi-Fi in both directions for
roughly seven hours a day — the user cannot reach Friday and Friday cannot
reach the user. The dashboard runs on the same Mac and talks to itself over
loopback, so it works with the Wi-Fi switched off entirely. Until this file
existed, Friday was mute for most of the user's waking hours.

IT IS A CHANNEL ADAPTER, NOT A SECOND CHAT IMPLEMENTATION — invariant 9. It
implements channels/base.py and nothing else. It does not read history, does
not build a request, does not call the model, does not decide what to say. It
holds the three methods and a sink, and channels/conversation.py drives it
with the identical code path Telegram runs. A card confirmed here and a card
confirmed in Telegram resolve the same pending_actions row.

WHY IT BUFFERS. A Telegram send is a POST to Telegram; there is somewhere to
put the text. A dashboard send has no such destination at the moment it
happens — the browser is not being spoken to, it is waiting on a response, or
on a stream. So every send becomes an EVENT: appended to this instance's list,
and handed to a sink if one was given. The HTTP route returns the list; the
sink is the SSE broadcast, which is how a card resolved in Telegram appears
in a browser that never asked for it.

ONE INSTANCE PER DELIVERY, not one per process. The event list is the record
of what this particular exchange produced, and a shared instance would mix two
users' turns — or, here, one user's chat turn with a card resolving from a
button tap. The sink is the shared part, and it is passed in.

SENDS NEVER FAIL. There is no network here. send() returns True because the
event is recorded; whether a browser is currently attached is not something
this layer can know and not something it should lie about either way. The
honest reading of True is "Friday said it", which is also what history
records.
"""

from __future__ import annotations

import logging
from datetime import datetime

from channels.base import Channel

logger = logging.getLogger("friday.dashboard.channel")

# Event kinds. Named rather than stringly-typed at each call site because the
# browser switches on them and a typo would be a silently-dropped message.
MESSAGE = "message"
CARD = "card"
NOTIFY = "notify"


class DashboardChannel(Channel):
    """One delivery's worth of output, plus an optional live sink."""

    name = "dashboard"

    def __init__(self, sink=None):
        self.events: list[dict] = []
        self._sink = sink

    # ── The contract ─────────────────────────────────────────────────────────

    def send(self, text: str) -> bool:
        return self._emit(MESSAGE, text=text)

    def send_permission_request(self, proposal: str, pending_key: str) -> bool:
        # VERBATIM, like every other channel. Invariant 3 — the buttons are
        # this surface's business, the text is not. The browser renders
        # `proposal` as-is and hangs Confirm/Cancel off `key`, which is the
        # same pending_actions id Telegram's callback_data carries.
        return self._emit(CARD, text=proposal, key=pending_key)

    def notify(self, title: str, text: str) -> bool:
        """Interrupt. Today this is an in-page event like any other, which
        reaches the user only if they are looking at the tab — the real
        out-of-browser interrupt lands with the notification commit. Written
        as a plain send rather than left unimplemented so the contract has no
        hole in it while that is true.

        LITERAL, never in persona (invariant 6).
        """
        return self._emit(NOTIFY, title=title, text=text)

    # ── Internals ────────────────────────────────────────────────────────────

    def _emit(self, kind: str, **payload) -> bool:
        event = {"kind": kind, "at": datetime.now().isoformat(), **payload}
        self.events.append(event)
        if self._sink is not None:
            # Wrapped: a broken stream must never fail the send behind it. The
            # event is already recorded, and history is written off the return
            # value — a sink failure that propagated would lose the message
            # from the transcript as well as from the browser.
            try:
                self._sink(event)
            except Exception as e:
                logger.debug(f"dashboard sink failed for {kind}: {e}")
        return True
