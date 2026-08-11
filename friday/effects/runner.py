"""
effects/runner.py
The only code that acts on a tool's behalf.

A tool declares what should happen; this file makes it happen. That is the
whole of invariant 2, and everything below follows from it.

ORDERING IS THE POINT OF THIS MODULE, NOT AN INCIDENTAL DETAIL.

    1. SendPermissionCard   — always first, unconditionally
    2. SendMessage
    3. everything else

Invariant 3 says permission cards are emitted first and nothing may delay,
editorialize on, or bury one. In Phase II that was a rule people had to
remember at every call site, and it was broken the ordinary way — by something
plausible being sent first. Here it is a sort inside one function, and a card
emitted last in a list is executed first. There is a test for exactly that,
because a rule with no test is a comment.

The card goes first even when it is the LAST thing a turn produced, and even
when a SendMessage in the same batch explains it. The user must see what they
are approving before they see anything about it — a card that arrives after a
paragraph of preamble is a card the user has already been talked into.

WHAT A CHANNEL HAS TO PROVIDE. Two synchronous methods:

    send(text) -> bool
    send_permission_request(proposal, pending_key) -> bool

channels/telegram.py::TelegramHandler already has both. The runner takes the
channel as an argument and never imports one — `grep -rn "channels" effects/`
finds nothing, and neither does the same grep over tools/.

SYNCHRONOUS, like everything below the channel. Both send methods are blocking
`requests` calls, so the caller runs this in an executor.

QUIPS ARE RENDERED HERE. A SendMessage may carry a quip_key naming what
happened ("<tool>:<outcome>"); the runner asks phrases.py for a line and
appends it. The tool never picks the words, and SendPermissionCard has no
quip_key at all — which is how "never a quip on a card" stops being a rule
anyone has to remember.

ONE FAILING EFFECT DOES NOT STOP THE REST. A batch is not a transaction and
cannot be made into one — the message that already went out cannot be recalled.
Failures are collected and reported; the loop keeps going. The alternative is
a failed quip suppressing the permission card behind it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tools.types import (CancelScheduled, Effect, ScheduleItem, SendMessage,
                         SendPermissionCard)

logger = logging.getLogger("friday.effects")


def _quip(quip_key: str) -> str:
    """Wrapped: a broken quip palette must never cost the user their message."""
    try:
        import phrases
        return phrases.quip_for_key(quip_key)
    except Exception as e:
        logger.debug(f"quip lookup failed for {quip_key!r}: {e}")
        return ""

# Lower runs first. The card's 0 is load-bearing; the rest is ordinary.
#
# An unknown effect sorts LAST rather than raising. A new effect type arriving
# without a runner branch is a bug, but it is a bug that must not be able to
# suppress the permission card sitting in the same batch.
_ORDER: dict[type, int] = {
    SendPermissionCard: 0,
    SendMessage: 1,
}
_LAST = 99


def order_key(effect: Effect) -> int:
    return _ORDER.get(type(effect), _LAST)


def sort_effects(effects) -> list[Effect]:
    """The execution order. Stable, so two messages keep the order the turn
    produced them in — only the card jumps the queue.

    Split out from run() so the ordering can be tested without a channel. The
    guarantee is about the order, not about the sending.
    """
    return sorted(effects, key=order_key)


@dataclass(frozen=True, slots=True)
class EffectReport:
    """What actually happened. Returned rather than logged-and-forgotten so a
    channel can tell whether the card it was told to show reached the user."""
    executed: int = 0
    failed: tuple[str, ...] = ()
    cards_sent: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed


@dataclass
class _Tally:
    executed: int = 0
    failed: list[str] = field(default_factory=list)
    cards: int = 0


def _run_one(effect: Effect, channel, tally: _Tally) -> None:
    if isinstance(effect, SendPermissionCard):
        # NOTHING IS PREPENDED, APPENDED, OR INTERPOLATED HERE. The proposal is
        # a template built by the tool from the arguments the user will be
        # approving. Persona voice does not apply to a card (invariant 6) and
        # neither does a quip.
        if channel.send_permission_request(effect.proposal, effect.pending_key):
            tally.executed += 1
            tally.cards += 1
        else:
            tally.failed.append(f"SendPermissionCard({effect.pending_key})")
        return

    if isinstance(effect, SendMessage):
        # THE QUIP IS APPLIED HERE, not by the tool. The tool named what
        # happened; choosing the words is rendering, and rendering is the
        # runner's job. It also means the rule "never a quip on a permission
        # card" needs no enforcing: this branch is the only place a quip is
        # ever added, and a card does not pass through it.
        #
        # An empty quip is a real answer — see phrases.py. Nothing is appended,
        # and there is no fallback line, because a quip that contradicts its
        # outcome is worse than no quip at all.
        text = effect.text
        if effect.quip_key:
            quip = _quip(effect.quip_key)
            if quip:
                text = f"{text} {quip}"
        if channel.send(text):
            tally.executed += 1
        else:
            tally.failed.append("SendMessage")
        return

    if isinstance(effect, (ScheduleItem, CancelScheduled)):
        # Defined, no consumer yet. Logged rather than raised: these cannot be
        # produced by anything today, and a raise here would be a crash for a
        # condition that cannot occur.
        logger.warning(
            f"{type(effect).__name__} has no runner branch yet — ignored. "
            f"It lands with the reminder scheduler."
        )
        return

    logger.error(f"Unknown effect {type(effect).__name__} — ignored.")
    tally.failed.append(f"unknown:{type(effect).__name__}")


def run(effects, channel) -> EffectReport:
    """Execute a turn's effects against a channel, card first.

    Returns what happened. Never raises: an effect that fails is reported, and
    the ones behind it still run. See the module docstring for why a batch is
    not a transaction.
    """
    tally = _Tally()
    for effect in sort_effects(effects):
        try:
            _run_one(effect, channel, tally)
        except Exception as e:
            logger.exception(f"Effect {type(effect).__name__} raised")
            tally.failed.append(f"{type(effect).__name__}: {e}")
    if tally.failed:
        logger.warning(f"Effects: {tally.executed} run, failed: {tally.failed}")
    return EffectReport(executed=tally.executed,
                        failed=tuple(tally.failed),
                        cards_sent=tally.cards)
