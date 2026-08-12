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
import shutil
import subprocess
from datetime import datetime

import compat
from channels.base import Channel

logger = logging.getLogger("friday.dashboard.channel")

# Event kinds. Named rather than stringly-typed at each call site because the
# browser switches on them and a typo would be a silently-dropped message.
MESSAGE = "message"
CARD = "card"
NOTIFY = "notify"


def _applescript_string(value: str) -> str:
    """A string literal for `osascript -e`.

    ESCAPED, NOT SANITISED. The body is Friday's own text, but it is text the
    model may have influenced, and an unescaped quote ends the literal and
    turns the rest of the message into AppleScript. Dropping the character
    instead would silently change what the user reads, which is the wrong
    trade for a notification whose entire job is to be accurate at a glance.

    Backslash first, then quote: doing it the other way round would escape the
    backslashes this function just added.
    """
    escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
        """Interrupt — reach the user when they are not looking at this
        surface. LITERAL, never in persona (invariant 6): an interrupt is not
        a reply, and a butler flourish on the one message that had to be
        readable at a glance is noise.

        TWO DOORS, BECAUSE NEITHER ONE IS ENOUGH ALONE.

        The stream event reaches an open dashboard, where the page raises a
        Web Notification whose onclick focuses the window. That is the only
        path that gives click-through, and it needs the app to be running.

        The macOS notification reaches the user when it is NOT running, which
        is the case the dashboard exists for — Telegram is blocked for seven
        hours a day and the tab is not always open. It does NOT click through:
        `display notification` is attributed to osascript, so clicking it
        activates that, not Friday. Accepted rather than worked around,
        because the alternatives are a bundled helper binary or a launch agent
        pretending to be an app, and the notification's job is to make the
        user look at the dock icon.

        True if EITHER door opened. The event is always recorded, so the
        return value is about the interrupt, not the record.
        """
        emitted = self._emit(NOTIFY, title=title, text=text)
        return self._os_notify(title, text) or emitted

    @staticmethod
    def _os_notify(title: str, text: str) -> bool:
        """A native notification, best-effort.

        Runs in the executor thread effects already run in, so a subprocess is
        fine — but it carries its own timeout regardless, because a hung
        osascript would hold that thread and the thread is shared.
        """
        if compat.IS_WINDOWS:
            return False
        osascript = shutil.which("osascript")
        if not osascript:
            return False
        try:
            script = (f"display notification {_applescript_string(text)} "
                      f"with title {_applescript_string(title or 'Friday')}")
            subprocess.run([osascript, "-e", script], timeout=10,
                           capture_output=True, check=True)
            return True
        except Exception as e:
            logger.debug(f"native notification failed: {e}")
            return False

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
