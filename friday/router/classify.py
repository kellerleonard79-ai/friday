"""
router/classify.py
Tier 2: one cheap call that picks a plan shape, and nothing else.

    classify(text, deadline=None) -> Plan | None

WHAT THIS COSTS AND WHAT IT SAVES. Measured on the live CLASSIFY profile
(gemini-3.5-flash-lite, temperature 0.0): ~255 input tokens, 3-8 output
tokens, median 577ms. A CHAT turn on the same message is ~2,600 input tokens
and 6-13s. The classifier is not free, and on a message that was going to need
CHAT anyway it is pure added latency — half a second and a quarter of a
kilotoken. What it buys is that ANSWER and CLARIFY turns are handed no tool
schemas at all, and that a READ_THEN_WRITE turn cannot write before it reads.

THE OUTPUT IS CONSTRAINED, NOT PARSED OUT OF PROSE. response_schema pins the
answer to one member of plans.names(). This was verified live before the
prompt was written, because an unconstrained classifier is a liability: it
puts a regex over model prose on the path that decides whether a turn may
write to the calendar. What comes back is JSON, so it is a QUOTED string —
'"ANSWER"', not 'ANSWER' — and json.loads is how it is read. A parser that
stripped quotes by hand would also happily accept a sentence with a plan name
in it, which is the failure this is supposed to make impossible.

MALFORMED IS NOT AN ERROR STATE, IT IS THE FALLBACK. Anything that is not one
of the five names — prose, a renamed plan, an empty string, a network failure,
a rate limit — returns None, and None means the turn runs exactly as it did
before this package existed: CHAT, its own tool scope, its own hop budget. The
router may narrow what a turn is allowed to do; it may never be the reason
Friday can do less than it could yesterday.

NO HISTORY, DELIBERATELY. The classifier sees one message and the injected
standing context (the clock, the machine's location — llm/dispatch.py adds
them to every call). It does not see the transcript. If picking between five
plan shapes needed twenty turns of context, the plan vocabulary would be
wrong, and the cost of being wrong here is bounded by the fallback anyway.
The one real casualty is the bare follow-up — "double it", "why not?" — which
has no shape of its own and lands on ANSWER. That is the correct answer often
enough and the wrong answer cheaply.

THE DEFINITIONS LIVE HERE, NOT IN AGENTS.md. Every other prompt fragment in
Friday is persona and belongs to the user, in a file they can edit while the
daemon runs. These are not voice — they name the plan shapes in
router/plans.py, and a user edit that renamed one would silently reroute every
message. Persona sections are prose about how to sound; this is a spec.
"""

from __future__ import annotations

import json
import logging

from llm import profiles
from llm.dispatch import dispatch
from llm.types import LLMRequest
from router import plans
from router.plans import Plan

logger = logging.getLogger("friday.router.classify")


# The plan definitions, as the model reads them.
#
# WRITTEN AGAINST THE OBSERVED FAILURES, not from first principles. A throwaway
# one-line prompt got two of five wrong in the Phase 0 probe, and one of them —
# "I have work on Wednesday from 6-9 pm" classified as ANSWER — sits in the
# largest single category of real traffic (31.7% of the corpus is a
# natural-language calendar write). So the first thing the definitions say
# about WRITE_DIRECT is that a statement of fact about the user's own schedule
# is a write. It reads like an odd thing to have to spell out, and it is the
# single most valuable line in the prompt.
_DEFINITIONS = """\
You are a router. Read the user's message and answer with exactly one plan \
name. Nothing else — no explanation, no punctuation, no reasoning.

The plans:

ANSWER
  Needs nothing from the calendar. Small talk, greetings, general knowledge, \
arithmetic, questions about you or how you work, weather, and follow-up \
questions that the conversation already answered.

READ_THEN_ANSWER
  A question whose answer requires looking at the user's calendar. Anything \
about what is scheduled, when something is, how busy a day is, or whether \
there is time for something.

READ_THEN_WRITE
  Changes, moves, reschedules, renames or cancels something ALREADY on the \
calendar, or adds something whose placement depends on what is already there \
("find me an hour on Thursday"). Use this whenever the target has to be \
identified before it can be touched.

WRITE_DIRECT
  Adds a NEW event the user has just described, with enough detail to place \
it. This includes plain statements of fact about the user's own schedule — \
"I have work Wednesday from 6 to 9", "I'm playing tennis at 7 tonight", \
"I've got a dentist appointment on the 20th at 3" are all WRITE_DIRECT. The \
user telling you when something is IS asking you to record it. It does not \
matter how casually it is phrased.

CLARIFY
  The user clearly wants something scheduled or changed, but a detail that \
cannot be guessed is missing — no date, no time, or no idea what the event \
is. Only use this when the missing piece genuinely cannot be inferred from \
the message. A relative date ("tomorrow", "Friday", "next week") is not \
missing information.

Examples:

  hello -> ANSWER
  what is 3 times 7 -> ANSWER
  is this Gemini? -> ANSWER
  will it rain today? -> ANSWER
  what am I doing next week? -> READ_THEN_ANSWER
  what is on my calendar on August 24th? -> READ_THEN_ANSWER
  when is the next time I work? -> READ_THEN_ANSWER
  how many events do I have on Friday? -> READ_THEN_ANSWER
  add team dinner on August 24th at 6:30pm -> WRITE_DIRECT
  put haircut on the calendar September 3rd at 10am -> WRITE_DIRECT
  book the physics review Friday morning at 8:15 -> WRITE_DIRECT
  I have work on Wednesday from 6-9 pm -> WRITE_DIRECT
  I'm going to play tennis at 7pm tonight -> WRITE_DIRECT
  add mom's birthday on September 12th, it's an all day thing -> WRITE_DIRECT
  move my dentist appointment to Thursday -> READ_THEN_WRITE
  cancel the robotics meeting -> READ_THEN_WRITE
  the location for tennis tomorrow is actually Bayview -> READ_THEN_WRITE
  find me an hour on Thursday for the physics review -> READ_THEN_WRITE
  put something on my calendar -> CLARIFY
  schedule a meeting with Sam -> CLARIFY

Message:
"""


def _schema() -> dict:
    """A constrained enum over the plan names, built from the table rather
    than restated. A schema listing plans that do not exist would be a prompt
    the model can satisfy and the code cannot resolve."""
    return {"type": "STRING", "enum": list(plans.names())}


def classify(text: str, deadline: float | None = None) -> Plan | None:
    """The plan for this message, or None to run the turn as CHAT always has.

    Never raises. A dispatcher error is a fallback, not an exception: the
    provider already returns finish="error" rather than raising, and the two
    remaining ways this could throw — an unconfigured dispatcher, a missing
    CLASSIFY profile — are startup bugs that must not take a chat turn with
    them.
    """
    try:
        response = dispatch(LLMRequest(
            profile=profiles.get("CLASSIFY"),
            prompt=_DEFINITIONS + (text or "").strip(),
            response_schema=_schema(),
            triggered_by="router",
            deadline=deadline,
        ))
    except Exception as e:
        logger.warning(f"Classifier dispatch failed ({e}) — falling back to CHAT.")
        return None

    if response.error_kind != "none":
        # Not reported to the user and not retried beyond what the dispatcher
        # already did. The message is about to be answered by CHAT, which will
        # hit the same network and produce the same sentence if it is really
        # down — and saying "the router is unavailable" would be describing an
        # internal component to someone who asked about their calendar.
        logger.info(
            f"Classifier unavailable ({response.error_kind}) — falling back to CHAT."
        )
        return None

    plan = plans.resolve(_decode(response.text))
    if plan is None:
        logger.warning(
            f"Classifier returned something that is not a plan "
            f"({response.text[:80]!r}) — falling back to CHAT."
        )
        return None

    logger.info(f"Router: {plan.name}")
    return plan


def _decode(raw: str) -> str | None:
    """The plan name out of a constrained response.

    JSON FIRST, because response_mime_type is application/json and the answer
    genuinely arrives quoted. The bare-string fallback exists for the provider
    that ignores the mime type — llm/providers/ollama.py has not been exercised
    since the rewrite — and is deliberately strict: it accepts a bare token and
    nothing else. It must not accept a SENTENCE containing a plan name, or the
    constrained-output guarantee is quietly replaced by a substring search.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        value = raw
    if not isinstance(value, str):
        return None
    value = value.strip().strip('"').strip()
    # One token, no spaces. "ANSWER" is a plan; "The plan is ANSWER" is prose.
    return value if value and " " not in value else None
