"""
channels/conversation.py
One user message, start to finish, on any channel.

    handle(text, channel, conn, config) -> Reply

WHY THIS IS NOT IN telegram.py ANY MORE. Everything a user message needs —
the gate, the pause check, the history window, the request, the executor hop,
the effects, the reply selection, the history writes — was inside
on_message(). All of it except `update.message` is channel-agnostic, and the
dashboard needs every line of it. Copying it would have produced a second
chat implementation, which invariant 9 forbids in exactly those words, and
would have meant that the next fix to reply selection had to be made twice.

So on_message() is now transport: pull the text off the update, call this,
and let the channel's own send() carry the answer. The dashboard route is the
same three lines against a different channel.

THE GATE LIVES HERE, AND IT IS ACQUIRED FIRST.

    async with TURN_GATE:

Nothing above it: no SQLite query, no context assembly, no model call. That
rule has not changed — what changed is which file it is written in, and it
moved for the same reason effects/entry.py exists. A rule that says "the top
of on_message()" protects one channel. There are two now, and a second
Semaphore(1) in the dashboard would serialize the dashboard against itself
while letting a dashboard turn and a Telegram turn interleave against the
same conversation_history. One gate, one queue, both surfaces.

WHY SERIALIZED AT ALL, since it is asked every time: history is read at the
start of a turn and written at the end. Two overlapping turns both read the
window before either writes, so the second one is answered as though the
first never happened — and then both append, producing a transcript in which
the model appears to have ignored a message. It is also a cost gate: two
concurrent turns are two concurrent tool loops.

THE CHANNEL SENDS. handle() decides WHAT is said and this module writes it to
history exactly once; the channel decides HOW it arrives. Telegram used
update.message.reply_text() for this, which is why the split was invisible —
it looked like transport but it was the only path prose took to the user.

PAUSE IS REPORTED, NOT ACTED ON. Telegram drops a paused message silently, so
the user can resume and replay; the dashboard says so, because the user is
looking at a text box they just typed into and silence there is a bug report.
That difference is presentation, which is the channel's, so Reply carries the
fact and each channel decides.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

import memory.state as state
from agent.turn import run_turn
from effects import entry as effects_entry
from llm import profiles
from llm.types import LLMRequest

logger = logging.getLogger("friday.conversation")

# THE gate. One process, one queue, every channel. See the module docstring.
TURN_GATE = asyncio.Semaphore(1)

# Ceiling on any single executor call made while holding the gate. A backstop,
# not the budget: llm/dispatch.py enforces the CHAT profile's deadline and the
# provider clamps its HTTP timeout to what is left, so the deadline expires
# first by design. This stays because wait_for cannot kill an executor thread —
# if a blocking call ever hangs past its deadline anyway, this is what releases
# the pipeline (July 9 outage).
EXECUTOR_TIMEOUT_S = 150   # public: channels/telegram.py's media path shares it

_TIMEOUT_MSG = "Sorry, sir — that request timed out on my end. Try again?"
_EMPTY_MSG = "Sorry, sir — the model returned no text. Try again?"

# Keyed off LLMResponse.error_kind, never off an attribute reached out of the
# agent. The network wording is load-bearing: a blocked network has to be
# distinguishable from a generic failure, on both surfaces.
_ERROR_MSG = {
    "rate_limit": "I'm being rate limited, sir. Try again in a moment.",
    # Distinct from rate_limit on purpose: nothing the user did caused this and
    # nothing they can do fixes it. The dispatcher already retried.
    "transient": "The model service is having trouble on its end, sir. "
                 "Try again shortly.",
    "network": "I can't reach the model from this network.",
}


@dataclass(frozen=True, slots=True)
class Reply:
    """What happened, for the channel to present.

    `text` is what was said and is "" when a card went out — which is the
    correct shape for a gated write, not an empty response. NOTHING GOES IN
    THE ASSISTANT SLOT AFTER A CARD. Three phrasings were tried in step 4 and
    the model sent all three to the user verbatim on the next turn:
    "[permission card sent]", the card's own text, and a sentence describing
    the card. The pattern is not about wording — WHATEVER SITS IN THE
    ASSISTANT SLOT AFTER A CARD, THE MODEL WILL EVENTUALLY SAY. The
    assistant's turn for a card is the OUTCOME, written when the user taps.
    """
    text: str = ""
    cards: tuple[str, ...] = ()
    error_kind: str = "none"
    stopped_on: str = "answer"
    paused: bool = False

    @property
    def spoke(self) -> bool:
        return bool(self.text)


def pause_active(conn, what: str) -> bool:
    """Pause gate — the dashboard sets system_state.paused = "true". Handles
    timed auto-resume: clears the pause once paused_until has passed."""
    if state.get(conn, "paused") != "true":
        return False
    until = state.get(conn, "paused_until")
    if until:
        try:
            if datetime.fromisoformat(until) <= datetime.now():
                state.set(conn, "paused", "false")
                state.delete(conn, "paused_until")
                logger.info("Timed pause expired — resuming.")
                return False
        except ValueError:
            state.delete(conn, "paused_until")
    logger.info(f"Dropped while paused: {what}")
    return True


def _history(conn, config: dict) -> tuple:
    """The rolling window, oldest-first.

    NOT FILTERED BY CHANNEL, deliberately. A message typed into the dashboard
    has to be in scope when the user asks about it over Telegram an hour
    later — they are one conversation with one person, and splitting the
    window by surface would make Friday forget things for reasons the user
    cannot see. The channel column is a record of where a line came from, not
    a partition.
    """
    turns = (config.get("memory") or {}).get("short_term_turns", 20)
    rows = conn.execute(
        "SELECT role, content FROM conversation_history ORDER BY id DESC LIMIT ?",
        (turns * 2,)
    ).fetchall()
    return tuple((role, content) for role, content in reversed(rows))


async def handle(text: str, channel, conn, config: dict) -> Reply:
    """Run one user message. Sends the answer through `channel`.

    Never raises for a model- or tool-level failure — those arrive as an
    error_kind and become a sentence.
    """
    text = (text or "").strip()
    if not text:
        return Reply()

    async with TURN_GATE:
        if pause_active(conn, f"{getattr(channel, 'name', '?')}: {text[:80]}"):
            return Reply(paused=True)

        state.set_many(conn, {
            "last_message_at":      datetime.now().isoformat(),
            "last_message_preview": text[:80],
        })
        logger.info(f"Message ({getattr(channel, 'name', '?')}): {text[:80]}")

        request = LLMRequest(
            profile=profiles.get("CHAT"),
            prompt=text,
            history=_history(conn, config),
            triggered_by="user_message",
        )

        loop = asyncio.get_running_loop()
        try:
            # run_turn() is blocking by design — the model call and the calendar
            # reads must not run on the event loop. The WHOLE turn goes into one
            # executor call so the tool cache and the fact ledger, which are
            # per-turn objects the loop creates and passes down, live and die
            # with it. (They were thread-locals once; tools run in a worker pool
            # and could not see them, so every precondition failed closed.)
            result = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: run_turn(request, conn)),
                timeout=EXECUTOR_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                f"handle: the turn was still running after {EXECUTOR_TIMEOUT_S}s "
                f"— releasing the pipeline. text={text[:80]!r}"
            )
            effects_entry.log_history(conn, "user", text, channel=channel)
            channel.send(_TIMEOUT_MSG)
            effects_entry.log_history(conn, "assistant", _TIMEOUT_MSG,
                                      channel=channel)
            return Reply(text=_TIMEOUT_MSG, error_kind="fatal",
                         stopped_on="error")

        if result.tool_calls_made:
            logger.info(
                f"Turn used {result.tool_calls_made} tool call(s) over "
                f"{result.hops} hop(s), ended on {result.stopped_on}."
            )

        effects_entry.log_history(conn, "user", text, channel=channel)

        # EFFECTS RUN BEFORE THE MODEL'S OWN REPLY, and that ordering is
        # invariant 3 rather than a preference. A permission card has to reach
        # the user before anything that could editorialize on it, and the
        # model's reply is exactly such a thing. effects/runner.py puts the card
        # first WITHIN the batch; this puts the whole batch ahead of the prose.
        #
        # Blocking sends, so an executor. The turn's own deadline is already
        # spent by this point, which is why this is not inside it.
        cards: tuple[str, ...] = ()
        effects_spoke = False
        if result.effects:
            report = await loop.run_in_executor(
                None, lambda: effects_entry.deliver(result.effects, channel, conn)
            )
            cards = report.card_texts
            # Whether ANYTHING reached the user, not just a card. The old test
            # was `card_texts` alone, which was right for the only tool that
            # existed: add_calendar_event always emits a card. A tool that
            # emits only a SendMessage — and several will — would say its piece
            # and then be followed by "the model returned no text", because
            # nothing downstream knew the user had already been answered.
            effects_spoke = report.executed > 0
            if not report.ok:
                logger.warning(f"Effects partially failed: {report.failed}")

        if cards:
            # A CARD WAS SENT, SO NOTHING FOLLOWS IT. Not the model's trailing
            # prose, not an error line. Invariant 3 — nothing may delay,
            # editorialize on, or bury a permission card — and a sentence
            # after one is the commonest way to editorialize on it.
            #
            # This is not hypothetical. The first card the dashboard produced
            # was followed by "观察到您已调用工具，请在确认卡片中确认该日程" —
            # the model narrating its own tool call, in Chinese, underneath a
            # card that already said everything. The branch used to run only
            # when result.text was empty, so the model's willingness to add a
            # line was the only thing keeping the rule.
            #
            # Suppressed, not sent-and-not-logged: a sentence the user reads is
            # a sentence that belongs in history, so the only correct handling
            # of prose that must not be read is to not send it.
            if result.text:
                logger.info(
                    f"Suppressed prose after a card ({len(result.text)} chars): "
                    f"{result.text[:120]!r}"
                )
            if result.error_kind != "none":
                logger.warning(
                    f"LLM {result.error_kind} on a turn that emitted a card — "
                    f"not reported to the user, the card stands: "
                    f"{result.error_message}"
                )
            said = ""
        elif result.error_kind != "none":
            said = _ERROR_MSG.get(result.error_kind,
                                  f"LLM error, sir: {result.error_message}")
            logger.warning(f"LLM {result.error_kind} — sending to user: {said}")
        elif result.text:
            said = result.text
        elif effects_spoke:
            # The turn's effects already answered the user — a tool's own
            # SendMessage — and the model added nothing on top. That is an
            # answer, not an empty response.
            #
            said = ""
        else:
            said = _EMPTY_MSG
            logger.warning("Empty LLM response — sending the fallback.")

        if said:
            channel.send(said)
            effects_entry.log_history(conn, "assistant", said, channel=channel)

        return Reply(text=said, cards=cards, error_kind=result.error_kind,
                     stopped_on=result.stopped_on)
