"""
channels/base.py
What a channel is. Three methods, and nothing about transport.

This contract already existed — it was prose in effects/runner.py's
docstring, which is where a contract goes to be almost true. There was one
implementation, so nothing tested the claim, and `notify` did not exist at
all even though the interrupt path has always needed it.

    send(text)                            -> bool
    send_permission_request(proposal, key) -> bool
    notify(title, text)                    -> bool

A channel is a TRANSPORT. It formats for its surface and it delivers. It does
not decide what to say, does not consult the model, does not touch the
database, and does not decide whether something should be sent — policy lives
above it and the effects runner decides ordering. A grep for "dispatch" or
"profiles" under channels/ should stay empty of anything but a request being
built at the entry point.

EVERY METHOD RETURNS A BOOL AND NEVER RAISES. A channel that raises takes the
turn down with it, and the whole reason effects/runner.py can promise "one
failing effect does not stop the rest" is that a failed send reports itself
instead of unwinding. False means the user did not get it.

WHY notify IS SEPARATE FROM send. On Telegram they are the same thing — the
transport IS a push notification, and there is nothing to distinguish. On the
dashboard they are not: send() puts a line in a chat the user may not be
looking at, and notify() has to cross out of the browser entirely. A channel
that only had send() would make the dashboard useless for the one job it
exists for, which is reaching the user when Telegram cannot.

NOTIFICATIONS ARE LITERAL. Invariant 6: persona voice applies to
conversational replies only. A notification is not a reply, it is an
interrupt, and a butler flourish on an interrupt is noise on the one message
that had to be readable at a glance.

SYNCHRONOUS. Every implementation is a blocking call — requests.post, or a
subprocess. Callers run them in an executor; effects/entry.py is already
called that way. An async channel would put a blocking HTTP call back on the
event loop, which is the July 9 outage in a different costume.

STRUCTURAL, NOT NOMINAL — but implementations say so anyway. Channel is a
runtime_checkable Protocol, so effects/runner.py keeps accepting anything with
the right shape (the tests pass a FakeChannel, and effects/entry.py wraps a
channel in a proxy). Real implementations subclass it explicitly regardless:
the methods are abstract, so a channel that forgets one fails at construction
rather than at 2am when the first card needs sending.

`isinstance(x, Channel)` works; `issubclass` does not, because `name` is a
data member and typing refuses issubclass on protocols that have one. That is
a limitation of the check, not of the contract — use isinstance, or check the
MRO for an explicit implementation.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable


@runtime_checkable
class Channel(Protocol):
    """The surface Friday talks to a user through."""

    #: Short, stable identifier — "telegram", "dashboard". Goes into
    #: conversation_history so a message typed in one channel is legible as
    #: such from the other. Not a display name; nothing renders it.
    name: str = "channel"

    @abstractmethod
    def send(self, text: str) -> bool:
        """Deliver one message. True if the user got it.

        Plain text. A channel may escape for its own surface, but it must not
        add to, wrap, or reorder what it was handed — the words were chosen
        above it.
        """
        ...

    @abstractmethod
    def send_permission_request(self, proposal: str, pending_key: str) -> bool:
        """Present a permission card: the proposal, and a way to confirm or
        cancel it against `pending_key`.

        THE PROPOSAL IS SENT VERBATIM. Invariant 3 — nothing may delay,
        editorialize on, or bury a card, and a banner above the proposal is
        the smallest possible version of burying it. No persona, no quip, no
        preamble. The buttons are the channel's business; the text is not.

        `pending_key` is the row in pending_actions. Both channels use the
        same keys, which is what makes a card confirmed in one resolve in the
        other.
        """
        ...

    @abstractmethod
    def notify(self, title: str, text: str) -> bool:
        """Interrupt the user — reach them when they are not looking at this
        surface. True if the interrupt was raised.

        Literal, never in persona. See the module docstring.
        """
        ...
