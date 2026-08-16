"""
router/fastpath.py
Tier 1: the answers that need no model at all.

    match(text)                 -> FastMatch | None      pure, no I/O
    respond(m, conn, config)    -> str | None            does the work
    answer(text, conn, config)  -> str | None            both, from async

WHY THIS IS WORTH THE PATTERNS. Three payoffs, and each would carry the module
on its own:

  COST. Every hit is a turn that never reaches an LLM. A greeting answered by
  CHAT costs ~2,600 input tokens and 6-13s, measured off llm_exchanges; a
  greeting answered here costs a dict lookup.

  OFFLINE. A pattern match works with the Wi-Fi off, and Telegram is blocked
  on school Wi-Fi for roughly seven hours of most days. Before this, a dead
  network meant Friday did nothing. Now it means Friday does less.

  FAILURE SURFACE. Gemma re-proposes a card for an event from an earlier turn
  about one turn in eight. That is a whole-turn failure mode, and a request
  that never reaches CHAT cannot produce one. This does not fix that behavior;
  it shrinks how often it can happen.

BUILT AGAINST THE TRAFFIC, NOT AGAINST A GUESS. The patterns below come from
183 real user messages recovered from logs/friday.log — conversation_history
holds 48 rows and is not the transcript of record. The measured share of that
corpus each pattern catches is written next to it, including the ones that
catch almost nothing and are here for offline reasons instead.

TWO RULES, AND THE FIRST IS THE ONE THAT MATTERS.

  A MATCH MUST BE UNAMBIGUOUS. Every pattern is a fullmatch against the whole
  normalized message. A near-miss falls through to tier 2 — "will it rain
  today?" is a match and "should I bring a jacket to practice?" is not, and
  the second must reach the model. A wrong fast-path match is worse than a
  slow correct one: it is a confident wrong answer with no model in the loop
  to hedge, delivered faster than the right one would have been.

  RESPONSES ARE TEMPLATES, NOT MODEL OUTPUT. Nothing here is generated. The
  greeting draws a curated line from quips.yaml, which is authored voice
  rather than model output; everything else is a format string over data.

FALLING THROUGH IS ALWAYS AVAILABLE, AND IT IS NOT A FAILURE. respond()
returns None whenever it cannot answer well — a cold weather cache, a calendar
that will not read, an empty greeting palette. The caller treats None exactly
as it treats "no match", so the worst case of this whole module is that the
message costs a model call, which is what it cost before.

NO SLASH COMMANDS, DELIBERATELY. friday.py registers
MessageHandler(filters.TEXT & ~filters.COMMAND), so PTB drops "/brief" before
any Friday code runs, while the dashboard has no such filter and would accept
it. A pattern that works on one surface and silently does nothing on the other
is worse than no pattern. Bare words only, on both.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta

import clock
import compat
import memory.state as state
import phrases

logger = logging.getLogger("friday.router.fastpath")


@dataclass(frozen=True, slots=True)
class FastMatch:
    """Which pattern caught the message, and anything it captured.

    Separated from the answer so that MATCHING IS PURE. The whole
    near-miss-falls-through rule is a claim about matching, and a claim about
    matching is only cheap to test when matching does no I/O and needs no
    database. tests/test_fastpath.py exercises match() against the entire
    historical corpus without a config, a connection, or a network.
    """
    pattern: str
    args: tuple[str, ...] = ()
    # THE ORIGINAL WORDS, carried because one responder genuinely needs them:
    # connectors/weather.py parses the query itself to find "at 3pm" or
    # "tomorrow", so handing it only the pattern name would answer every
    # weather question as though it were about right now.
    text: str = ""


# ── Normalisation ────────────────────────────────────────────────────────────
#
# Deliberately shallow. Every transformation here widens what matches, and
# every widening is a chance to catch something that should have gone to the
# model. Case, surrounding whitespace, curly apostrophes and trailing
# sentence punctuation are noise in every message ever sent. NOTHING ELSE IS
# TOUCHED — no stemming, no stopword removal, no politeness stripping, no
# spelling correction. "what is the percent chance of rain acccording to your
# data?" is a real message from the corpus and it SHOULD fall through: it is
# asking about the source, not about the rain.

_TRAILING = " \t.!?…,"


def _norm(text: str) -> str:
    t = (text or "").strip().lower()
    t = t.replace("’", "'").replace("‘", "'")
    t = re.sub(r"\s+", " ", t)
    return t.strip(_TRAILING)


# Politeness that can trail any message without changing it. Applied as an
# optional suffix inside each pattern rather than stripped up front, so a
# pattern can decline it — and so "sir" alone still has to be matched on
# purpose rather than becoming the empty string and matching everything.
_SIR = r"(?:[, ]+(?:sir|jarvis|friday))*"


def _p(body: str) -> re.Pattern:
    return re.compile(rf"(?:{body}){_SIR}", re.IGNORECASE)


# ── The patterns ─────────────────────────────────────────────────────────────
#
# Percentages are the share of the 183-message historical corpus each pattern
# catches, measured by tests/test_fastpath.py against corpus_shapes.txt.

# Every "what's" a user has ever actually typed. Spelled out rather than made
# optional-apostrophe-clever, because "whats" with no apostrophe is the single
# commonest spelling in the corpus and a pattern that missed it would have
# looked like it worked in every test written by someone who punctuates.
_W = r"(?:what's|whats|what is|what)"


_GREETING = _p(
    r"(?:hello|hi|hey|yo|heya|good (?:morning|afternoon|evening|day)"
    r"|(?:are )?you there|are you (?:there|awake|alive|up)"
    r"|you (?:awake|alive|up)|sir|jarvis|friday)"
)

_BRIEF = _p(
    r"(?:brief me(?: now)?|(?:the |my )?(?:morning |evening )?brief(?:ing)?"
    r"|" + _W + r" my day(?: look(?: like)?)?"
    r"|how does (?:today|my day) look"
    r"|what does (?:today|my day) look like)"
)

# WEATHER is the pattern most at risk of over-reaching, so it is a whitelist of
# whole questions rather than a keyword test. "will it rain today" is weather;
# "should I bring a jacket to practice" is a judgement about a specific event
# and belongs to the model, even though a keyword test would call both weather.
_TIME_TAIL = r"(?: (?:today|tonight|tomorrow|right now|outside|later|this (?:morning|afternoon|evening)))?"
_AT_TIME = r"(?: at \d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
# Either order. "will it rain at 3pm today" and "will it rain today at 3pm" are
# the same question and the corpus contains the first spelling, which a
# tail-then-time pattern misses in a way nobody notices until it is live.
_WHEN = _AT_TIME + _TIME_TAIL + _AT_TIME

_WEATHER = _p(
    r"(?:weather"
    r"|" + _W + r"(?: the)? weather(?: like)?" + _WHEN +
    r"|how(?:'s| is)(?: the)? weather" + _WHEN +
    r"|rain"
    r"|will it rain" + _WHEN +
    r"|is it (?:going to|gonna) rain" + _WHEN +
    r"|is it raining" + _WHEN +
    r"|will there be (?:rain|precipitation)" + _WHEN +
    r"|" + _W + r" the (?:chance|percent chance|probability|odds) of rain"
    r"(?: in the next \d+ hours)?" + _WHEN +
    r"|how (?:hot|cold|warm) is it" + _WHEN +
    r"|" + _W + r" the (?:temperature|temp)" + _WHEN +
    r")"
)


# CALENDAR is restricted to an explicit today/tomorrow and nothing else. Not
# for the hit rate, which is poor — it is the one calendar capability that
# survives a dead network, and "what's on my calendar today" is the thing you
# most want to still work when the API is unreachable. A bare "what's on my
# calendar" is NOT here: it has no date in it, and guessing which day someone
# meant is exactly the confident-wrong-answer this module refuses to produce.
_CALENDAR = _p(
    r"(?:" + _W + r"(?: on)? my (?:calendar|schedule)(?: for)? (today|tomorrow)"
    r"|what do i have(?: on my (?:calendar|schedule))?(?: for)? (today|tomorrow)"
    r"|what have i got(?: on my (?:calendar|schedule))?(?: for)? (today|tomorrow)"
    r"|what am i doing (today|tomorrow)"
    r"|(?:list|show me)(?: everything on)? my (?:calendar|schedule)(?: for)? (today|tomorrow)"
    r"|my (?:calendar|schedule)(?: for)? (today|tomorrow)"
    r"|(today|tomorrow)'?s (?:calendar|schedule)"
    r")"
)


_PAUSE = _p(r"(?:pause|pause yourself|go quiet|stand down)")


# Ordered. The first fullmatch wins, and the order only matters where two
# patterns could both fire — which today they cannot, since every one is a
# fullmatch over disjoint whole-message forms. Kept explicit anyway: the day
# someone widens one of these, "which won" must be readable rather than
# whatever the dict happened to hold.
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("greeting", _GREETING),
    ("brief",    _BRIEF),
    ("weather",  _WEATHER),
    ("calendar", _CALENDAR),
    ("pause",    _PAUSE),
)


def match(text: str) -> FastMatch | None:
    """Which tier-1 pattern this message is, or None to fall through.

    fullmatch, always. A search() here would turn "add lunch, and what's the
    weather" into a weather answer with the calendar write silently dropped,
    which is the single worst thing this module could do.
    """
    norm = _norm(text)
    if not norm:
        return None
    for name, pattern in _PATTERNS:
        m = pattern.fullmatch(norm)
        if m is None:
            continue
        args = tuple(g for g in (m.groups() or ()) if g)
        return FastMatch(pattern=name, args=args, text=text)
    return None


# ── Responders ───────────────────────────────────────────────────────────────


def _clock_phrase(config: dict) -> str:
    """"3:40 PM on a Wednesday". compat.strftime because %-I is glibc-only."""
    now = clock.local_now(config)
    return compat.strftime(now, "%-I:%M %p on a %A")


def _greeting(config: dict) -> str | None:
    """A curated line plus the clock.

    THE CLOCK IS WHY THIS IS NOT DEADENING. A canned greeting is a canned
    greeting on the twentieth repeat; a canned greeting carrying the actual
    time is an answer. The clock is already injected into every model prompt
    (llm/context.py) — this is the same fact reaching the user through the
    cheaper door.

    An empty palette returns None rather than a bare timestamp: the honest
    handling of "there is nothing to say" is to let the model say it.
    """
    line = phrases.greeting()
    if not line:
        return None
    return f"{line} It's {_clock_phrase(config)}."


# The weather cache. connectors/weather.py is stateless and every call is two
# HTTPS round trips, so without this a repeated "will it rain today" pays full
# price each time — and the corpus has that exact question six times.
#
# TWO AGES, BECAUSE THEY ANSWER DIFFERENT QUESTIONS. Under _FRESH_S the cached
# answer is served outright. Past it a live fetch is attempted. Past _MAX_AGE_S
# the cached answer is not used AT ALL, even if the fetch fails — a stale
# forecast delivered confidently is worse than falling through to the model,
# which is worse than nothing only if you have never been rained on.
_FRESH_S = 600.0        # 10 minutes: serve from cache
_MAX_AGE_S = 1800.0     # 30 minutes: beyond this, the cache does not exist
_weather_cache: dict[str, tuple[float, str]] = {}


def _weather(text: str, config: dict) -> str | None:
    """A weather answer, or None to fall through.

    THIS ONE FETCHES, AND IT IS THE ONLY THING HERE THAT DOES. That is a real
    cost and it is stated rather than hidden: it runs while conversation.py's
    TURN_GATE is held, so a slow OpenWeatherMap stalls every queued message.
    It is bounded — connectors/weather.py uses a 10s timeout on each of at
    most two requests — and the alternative was worse in both directions. A
    warm-on-a-timer cache like connectors/location.py cannot serve this,
    because a weather question carries its own time ("will it rain at 3pm")
    and one warmed value answers only one of them.

    NOT A VIOLATION OF "INJECT, NEVER FETCH". That rule (llm/context.py,
    llm/dispatch.py) governs what gets built into a PROMPT, and its reason is
    that a model which has to ask can decline to ask. Nothing here reaches a
    model. What it shares with the rule is the gate, which is why the bound
    matters.

    WORTH KNOWING: CHAT cannot answer a weather question at all today. The
    weather tool went with the Phase II teardown, no weather block is
    injected, and connectors/weather.py's own docstring still claims it is
    "called on demand from on_message" — which stopped being true at the
    teardown. So this responder is not a cheaper path to an existing answer.
    It is the only path to one, and until it existed every weather question in
    the corpus was being answered by a model with no weather data in front of
    it.
    """
    cfg = config.get("weather") or {}
    if not cfg:
        return None

    # Keyed on the question, not on an intent guess: connectors/weather.py
    # parses the query itself, so two differently-worded questions can produce
    # genuinely different answers and must not share a cache slot.
    key = _norm(text)
    hit = _weather_cache.get(key)
    now = time.monotonic()
    if hit and (now - hit[0]) < _FRESH_S:
        logger.info("Fast path: weather from cache.")
        return hit[1]

    try:
        from connectors import weather

        answer = weather.respond(cfg, text)
    except Exception as e:
        logger.warning(f"Fast-path weather failed: {e}")
        answer = ""

    if answer:
        _weather_cache[key] = (now, answer)
        return answer

    # The fetch failed. A cache entry inside the hard bound is still not used:
    # see the two-ages comment above.
    logger.info("Fast path: no weather available — falling through to the model.")
    return None


def _calendar(day_word: str, conn, config: dict) -> str | None:
    """Today's or tomorrow's schedule, rendered without a model.

    BUILT FOR OFFLINE, NOT FOR COST. It catches about 2% of the corpus, which
    on its own would not justify the code. It earns its place because the
    calendar backend is local — EventKit reads the on-disk store, JXA talks to
    Calendar.app — so this answers with the network entirely down, and "what
    is on my calendar today" is the thing you most want working then.

    The tool function is called DIRECTLY rather than through
    tools/executor.py. There is no model, so there is nothing to validate
    arguments against and nothing to record a ledger for: the ledger exists so
    a precondition can ask what a MODEL established this turn, and no model is
    involved. The arguments are constructed here, from a matched literal.
    """
    from tools import calendar_read
    from tools.types import ToolError

    now = clock.local_now(config)
    day = now.date() + (timedelta(days=1) if day_word == "tomorrow" else timedelta())
    iso = day.isoformat()

    outcome = calendar_read.get_schedule(date_from=iso, date_to=iso)
    if isinstance(outcome, ToolError):
        logger.info(f"Fast path: calendar unreadable ({outcome.kind}) — falling through.")
        return None

    events = outcome.data.get("events") or []
    label = "tomorrow" if day_word == "tomorrow" else "today"
    if not events:
        return f"Nothing on your calendar {label}, sir."

    timed = [e for e in events if not e.get("all_day")]
    all_day = [e for e in events if e.get("all_day")]

    lines = [f"On your calendar {label}, sir:"]
    for e in sorted(timed, key=lambda e: e.get("start") or ""):
        when = _time_of(e.get("start"))
        title = (e.get("title") or "").strip() or "(untitled)"
        where = f" — {e['location']}" if e.get("location") else ""
        lines.append(f"• {when} {title}{where}" if when else f"• {title}{where}")
    for e in all_day:
        title = (e.get("title") or "").strip() or "(untitled)"
        lines.append(f"• All day: {title}")
    return "\n".join(lines)


def _time_of(iso: str | None) -> str:
    """"3:40 PM" from an ISO local timestamp, or "" if it will not parse."""
    if not iso:
        return ""
    try:
        from datetime import datetime

        return compat.strftime(datetime.fromisoformat(iso), "%-I:%M %p")
    except Exception:
        return ""


def _pause(conn) -> str:
    """Pause from chat.

    THERE IS NO "RESUME" PATTERN, AND THAT IS NOT AN OVERSIGHT. A paused
    Friday drops the message in conversation.handle()'s pause check, which
    runs BEFORE the router — so a "resume" pattern could never be reached. The
    fix is not to hoist the router above the pause check: that would let every
    tier-1 pattern answer while paused, which is precisely what pausing is
    for.

    So the confirmation says where the other end of the switch is. A pause
    with no visible way back is a worse feature than no pause.
    """
    state.set(conn, "paused", "true")
    state.delete(conn, "paused_until")
    return ("Paused, sir. I'll keep listening and say nothing. "
            "Resume me from the dashboard or the menu bar when you want me back.")


def respond(m: FastMatch, conn, config: dict) -> str | None:
    """Answer a match, or None to fall through to the model.

    Synchronous: every branch either does no I/O or does blocking I/O, and the
    caller runs the whole thing in an executor. answer() below is the door
    from async code.
    """
    if m.pattern == "greeting":
        return _greeting(config)
    if m.pattern == "weather":
        return _weather(m.text, config)
    if m.pattern == "calendar":
        return _calendar(m.args[0] if m.args else "today", conn, config)
    if m.pattern == "pause":
        return _pause(conn)
    # "brief" is not here: it is async, because it shares a lock with the
    # dashboard button. See answer().
    return None


def _briefing(conn, config: dict) -> str | None:
    """The on-demand briefing, composed in process.

    THE SAME TWO CALLS friday.py::send_on_demand_briefing makes, minus the
    Telegram push — and the push is the whole difference. That function exists
    to answer a dashboard BUTTON, which has no channel of its own, so it
    delivers to Telegram. This is answering a MESSAGE, and the channel that
    received the message is the channel that answers it (rule 23). Routing a
    briefing asked for in the dashboard out through Telegram is the exact bug
    step 5 fixed for permission cards.

    NO MODEL. compose_on_demand has been a deterministic renderer since the
    teardown — header plus the bundle's sections — so this whole path is
    already free, and "brief me" was reaching CHAT with tools for no reason
    other than that nothing had told it not to.

    Deliberately does not call _record_briefing_sent, for the same reason
    friday.py does not: that would set the slot's sent-latch and make the real
    scheduled briefing skip itself for the rest of the day.
    """
    from agent import briefings

    bundle = briefings.bundle_briefing_context("on_demand", config, conn)
    text = briefings.compose_on_demand(None, bundle)
    return text or None


async def answer(text: str, conn, config: dict) -> tuple[str, str] | None:
    """The whole of tier 1, from async code: (reply, pattern) or None.

    None means "the model handles this" and covers both "no pattern matched"
    and "a pattern matched but could not answer well". The caller must not
    distinguish them — a cold weather cache and an unrecognised sentence are
    the same instruction to fall through.
    """
    m = match(text)
    if m is None:
        return None

    loop = asyncio.get_running_loop()

    if m.pattern == "brief":
        # LOCK ORDER: TURN_GATE (held by our caller) → ON_DEMAND_LOCK. Never
        # the reverse — the dashboard button takes ON_DEMAND_LOCK and never
        # touches TURN_GATE, so the two never nest in the other direction and
        # nothing may make them. See agent/briefings.py::ON_DEMAND_LOCK.
        from agent import briefings

        async with briefings.ON_DEMAND_LOCK:
            said = await loop.run_in_executor(None, lambda: _briefing(conn, config))
    else:
        said = await loop.run_in_executor(None, lambda: respond(m, conn, config))

    if not said:
        logger.info(f"Fast path: {m.pattern} matched but could not answer — "
                    f"falling through to the model.")
        return None

    # Logged distinctly so the hit rate is measurable off the log alone, the
    # way the corpus this was built from had to be.
    logger.info(f"Fast path HIT [{m.pattern}]: {text[:60]!r}")
    return said, m.pattern
