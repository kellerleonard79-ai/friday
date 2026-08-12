"""
effects/entry.py
The one door effects go out of.

    deliver(effects, channel, conn) -> EffectReport

WHY THIS EXISTS. effects/runner.py orders and executes a batch; that part was
never the problem. The problem is what has to happen ALONGSIDE the execution
and is not part of it: every sentence that reaches the user has to reach
conversation_history too, because the model's next request is built from those
rows and a sentence missing from them is a sentence that, as far as the model
is concerned, was never said.

That obligation was discharged twice, in two unrelated ways:

  channels/telegram.py  called runner.run() and then wrote history inline,
                        several branches later, and did NOT log the prose a
                        tool's own SendMessage produced — only the model's
                        reply.

  effects/pending.py    wrapped the channel in a private _LoggingChannel so
                        that everything the confirm path said was logged.

Neither was reusable by a third caller, and the dashboard is the third caller.
Step 4 already shipped the version of this bug where one call site simply
forgot: the proposing turn recorded "I put a card in front of you", the reply
saying the event was added went only to Telegram, and the model — looking at a
proposal that never resolved — proposed it again. One message produced three
cards.

So the runner call and the logging are welded together here, and neither
caller may call runner.run() directly. `grep -rn "runner.run(" --include=*.py`
outside this file should find nothing but the tests.

WHAT IT DOES NOT TAKE. No request, no deadline, no ledger. The confirm path
has none of the three — it arrives from a button tap, not from a turn, and
requiring them would mean fabricating a turn around a decision that has
already been made. Effects and a channel is the whole input, because that is
the whole of what the two callers have in common.

CARDS ARE NOT LOGGED AS PROSE. A permission card is a question, not an
answer, and step 4 established the hard way that WHATEVER SITS IN THE
ASSISTANT SLOT AFTER A CARD, THE MODEL WILL EVENTUALLY SAY. The card's text
comes back on the report so the channel can see one went out; it does not go
into history. The assistant's turn for a card is the OUTCOME, written when the
user taps.

NEVER RAISES. Same contract as the runner: a lost history row must not cost
the user a message that already went out.
"""

from __future__ import annotations

import logging
from datetime import datetime

from effects import runner as effects_runner
from effects.runner import EffectReport

logger = logging.getLogger("friday.effects.entry")


def _channel_name(channel) -> str:
    """A Channel, a bare name, or nothing. Accepts all three because the
    callers legitimately have all three: conversation.py holds the channel
    object, the confirm path holds a wrapped one, and a caller with no channel
    at all (a job, a test) has neither."""
    if channel is None:
        return ""
    if isinstance(channel, str):
        return channel
    return str(getattr(channel, "name", "") or "")


def log_history(conn, role: str, text: str, channel=None) -> None:
    """Append one conversation_history row. Best-effort, by design.

    THE ONLY conversation_history WRITE UNDER effects/, and the only one any
    channel makes. Kept as a named function rather than inlined so the column
    list lives in one place — `channel` was added here and nowhere else.

    THE CHANNEL IS RECORDED, NOT USED TO PARTITION. The window every turn
    reads is unfiltered: a message typed in the dashboard has to be in scope
    when the user asks about it over Telegram. This column answers "where did
    this line come from", which is an operator question, not a model one.
    """
    if conn is None or not text:
        return
    try:
        conn.execute(
            "INSERT INTO conversation_history (role, content, created_at, channel) "
            "VALUES (?, ?, ?, ?)",
            (role, text, datetime.now().isoformat(), _channel_name(channel)),
        )
        conn.commit()
    except Exception as e:
        logger.debug(f"history logging failed: {e}")


class HistoryChannel:
    """A channel that also writes what it said to conversation_history.

    WRAPPING, NOT A FLAG THREADED THROUGH EVERY BRANCH. effects/pending.py has
    six places that talk to the user and one of them being forgotten is
    exactly how the open-loop bug appeared. A wrapper cannot be forgotten at a
    call site it does not know about.

    The wrapper sees the text AFTER the runner has appended any quip, which is
    the text the user actually read. Inspecting the effects instead would log
    a sentence nobody was sent.
    """

    def __init__(self, inner, conn):
        self._inner = inner
        self._conn = conn

    # Transparent for anything a caller reaches for that is not spelled out
    # below. Without this a wrapped channel quietly loses capabilities.
    def __getattr__(self, item):
        return getattr(self._inner, item)

    # Spelled out rather than left to __getattr__ because isinstance() against
    # a runtime_checkable Protocol looks at the class, not the instance — a
    # wrapper whose whole interface arrives through __getattr__ stops being a
    # Channel as far as any type check is concerned.
    @property
    def name(self) -> str:
        return getattr(self._inner, "name", "")

    def notify(self, title: str, text: str) -> bool:
        """Delegated and NOT logged. A notification is an interrupt, not a
        conversational turn — and it accompanies a send() that is logged, so
        logging it too would put the same sentence in history twice."""
        return self._inner.notify(title, text)

    def send(self, text: str) -> bool:
        ok = self._inner.send(text)
        if ok:
            log_history(self._conn, "assistant", text, channel=self._inner)
        return ok

    def send_permission_request(self, proposal: str, pending_key: str) -> bool:
        # Passed through and NOT logged — see the module docstring. A card is
        # not prose and must not sit in the assistant slot.
        return self._inner.send_permission_request(proposal, pending_key)


def as_history_channel(channel, conn):
    """Wrap once. Idempotent, because the confirm path wraps its channel for
    its own direct sends and then hands the same channel to deliver()."""
    if isinstance(channel, HistoryChannel):
        return channel
    return HistoryChannel(channel, conn)


def deliver(effects, channel, conn) -> EffectReport:
    """Execute a batch of effects against a channel, logging what was said.

    The single entry point into effects/runner.py. Card first — that ordering
    is the runner's, and this function does not reorder anything.
    """
    if not effects:
        return EffectReport()
    return effects_runner.run(effects, as_history_channel(channel, conn))
