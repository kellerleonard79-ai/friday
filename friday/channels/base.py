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

THE TAXONOMY IS SHARED; THE PHRASING IS THE CHANNEL'S.

failure_text() lives here because the sentence Friday says when something goes
wrong is presentation, and presentation is a channel's. It used to be a dict
in channels/conversation.py — ABOVE the channels — which meant a surface that
should phrase a failure differently had no say in it. Reply already carried
error_kind alongside the text, so the seam existed; nothing was on the other
side of it.

The KINDS stay shared and must: "rate limited", "the service is down" and
"this network cannot reach the API" are three different problems, and a
channel that collapsed two of them would be lying about which. What a channel
may change is the words, not the distinction.

This is small today — both channels take the default. It is here now for the
same reason effects/entry.py was: the version of this that is correct because
there is only one consumer is the version that is wrong the moment there are
two, and by then the fix is a refactor instead of a file move.

failure_text IS OPTIONAL AND IS NOT A MEMBER OF THE PROTOCOL. That is not
squeamishness — it was tried the other way round first and it broke the
contract. A runtime_checkable Protocol's isinstance() check tests EVERY
member, abstract or not, so putting a fourth method on Channel immediately
un-Channelled both duck-typed implementations: tests/test_channels.py's plain
object with the three methods, and effects/entry.py's HistoryChannel, whose
whole interface arrives through __getattr__. The protocol is the transport
contract and it is still three methods.

So a channel that wants its own words simply defines failure_text(kind,
detail); everything else inherits the default by not defining it. Callers ask
through failure_text_for() rather than calling the method directly, which is
what makes "optional" true rather than aspirational.

    def failure_text(self, kind: str, detail: str = "") -> str:

`kind` is the shared taxonomy below. A channel may change the WORDS for a
kind; it may not collapse two kinds into one sentence. `detail` is the raw
error line, for kinds with no useful generic sentence — diagnostic text that
may name internals.

Neither real channel overrides it today, and the dashboard specifically does
NOT: a retry banner is a step-7 conversation. What exists now is the seam, in
the place a channel can reach.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol, runtime_checkable

# The default sentence for each failure, keyed off the SHARED taxonomy — the
# llm/types.py ErrorKind values, plus the two conditions that arise in
# channels/conversation.py rather than under the dispatcher:
#
#   timeout — the executor call was abandoned at EXECUTOR_TIMEOUT_S. Not an
#             ErrorKind: the model never answered either way, so nothing below
#             classified it.
#   empty   — the turn finished cleanly, said nothing, and no effect said
#             anything either.
#
# Both are here rather than left behind in conversation.py because they are
# the same kind of thing as the other four — a sentence Friday says when
# something went wrong — and splitting them would leave a channel able to
# override four of six.
#
# THE `network` WORDING IS LOAD-BEARING. Telegram is blocked on school Wi-Fi
# for roughly seven hours a day; "I can't reach the model from this network"
# is the difference between the user knowing to switch surfaces and the user
# thinking Friday is broken. It must stay distinguishable from `transient`,
# which is Google having a bad day, and from `rate_limit`, which is quota.
DEFAULT_FAILURE_TEXT: dict[str, str] = {
    "rate_limit": "I'm being rate limited, sir. Try again in a moment.",
    # Distinct from rate_limit on purpose: nothing the user did caused this
    # and nothing they can do fixes it. The dispatcher already retried.
    "transient": "The model service is having trouble on its end, sir. "
                 "Try again shortly.",
    "network": "I can't reach the model from this network.",
    "timeout": "Sorry, sir — that request timed out on my end. Try again?",
    "empty": "Sorry, sir — the model returned no text. Try again?",
}


def default_failure_text(kind: str, detail: str = "") -> str:
    """The sentence for a failure kind, for a channel with no opinion.

    `fatal` and anything unrecognised fall through to the detail, because
    those are the cases where there is nothing useful to say generically and
    the raw line is more help than a soothing sentence that hides it.
    """
    sentence = DEFAULT_FAILURE_TEXT.get(kind)
    if sentence:
        return sentence
    return f"LLM error, sir: {detail}" if detail else "Something went wrong, sir."


def failure_text_for(channel, kind: str, detail: str = "") -> str:
    """Ask a channel how it phrases a failure, falling back to the default.

    getattr rather than a direct call because Channel is runtime_checkable and
    duck-typed implementations are supported on purpose — tests/test_channels.py
    asserts that a plain object with the three methods is a Channel. A
    duck-typed channel that never heard of failure_text still gets a sentence.
    """
    fn = getattr(channel, "failure_text", None)
    if callable(fn):
        try:
            return fn(kind, detail)
        except Exception:  # a channel's phrasing must not fail the reply
            pass
    return default_failure_text(kind, detail)


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
