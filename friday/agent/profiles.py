"""
agent/profiles.py
Call profiles: which slice of the persona each kind of LLM call carries.

Friday makes three shapes of LLM call, and until now all three paid for the
same system instruction — the whole of AGENTS.md plus every config-composed
block, on every call:

  CHAT     — the user is talking to Friday. Voice is the product and tools are
             live, so this profile carries everything.
  COMPOSE  — Friday writes prose the user will read (briefings, urgent
             alerts). Voice matters; the tool-usage sections do not, because
             these calls run with tools off by construction (see the module
             docstring in agent/briefings.py).
  CLASSIFY — Friday asks the model for a label, an index, or a JSON object
             (urgency tagging, GroupMe event extraction, media extraction,
             quip selection). Nothing here is read as prose. The persona is
             not merely wasted spend on this path — it is actively harmful: a
             butler voice bleeding into a call whose contract is to return
             exactly "URGENT" shows up downstream as a parse failure and a
             silently mis-tagged event.

Sections are addressed by their Markdown heading, so _MEMBERSHIP below is the
single place that decides what each profile carries. Add a heading to
AGENTS.md and it lands in CHAT and COMPOSE by default — see _DEFAULT.
"""

import logging

logger = logging.getLogger("friday.profiles")

CHAT = "chat"
COMPOSE = "compose"
CLASSIFY = "classify"
# ROUTE is the tool dispatcher's profile (agent/dispatcher.py). It is listed
# here for the contract, not for _render_persona: no persona section is mapped
# to it anywhere in _MEMBERSHIP, so it cannot pick up AGENTS.md prose or the
# config-composed blocks — including voice_phrases — even by accident. The
# dispatcher does not route through _think at all; it needs a different model,
# a different timeout and a response schema, none of which _think carries.
ROUTE = "route"

ALL = (CHAT, COMPOSE, CLASSIFY)

# The dispatcher's entire system instruction. Biased hard toward
# over-selection on purpose: an extra tool costs ~200 tokens of schema, while
# a missing one is a silent wrong answer — Friday chats about tennis instead
# of putting it on the calendar.
ROUTE_INSTRUCTION = (
    "You route a user message to the tools that might be needed to answer it. "
    "Reply with a JSON array of tool names and nothing else. When you are "
    "uncertain whether a tool applies, INCLUDE it — an extra tool is cheap, a "
    "missing one silently breaks the user's request. Reply with an empty array "
    "only when no tool could plausibly apply."
)

# What CLASSIFY sends instead of a persona. Deliberately not empty: Gemma with
# no system instruction at all drifts into conversational framing ("Sure! The
# urgency here would be...") and the callers of this path all parse the first
# token.
CLASSIFY_INSTRUCTION = (
    "You are a classification component inside a larger system. Follow the "
    "output format given in the message exactly. No preamble, no explanation, "
    "no commentary, no personality, no markdown."
)

# Heading (lowercased, '#' and whitespace stripped) → profiles that carry it.
# "" is the unheaded preamble at the top of AGENTS.md.
#
# The four CHAT-only sections are the tool-usage instructions. Every one of
# them names a tool the COMPOSE and CLASSIFY paths cannot call, so shipping
# them there buys nothing and spends ~450 tokens a call.
_MEMBERSHIP: dict[str, tuple[str, ...]] = {
    "":                               (CHAT, COMPOSE),
    "operational rules":              (CHAT, COMPOSE),
    "tone and address":               (CHAT, COMPOSE),
    "sourcing":                       (CHAT, COMPOSE),
    "urgency policy":                 (CHAT, COMPOSE),
    "scope":                          (CHAT, COMPOSE),
    "calendar writes":                (CHAT,),
    "editing vs. adding":             (CHAT,),
    "calendar title hygiene":         (CHAT,),
    "self-editing":                   (CHAT,),
    "voice":                          (CHAT, COMPOSE),
    "where the voice does not apply": (CHAT, COMPOSE),
    # Blocks composed from config by agent/core.py::_persona_blocks. Keyed the
    # same way, so the two sources of persona text are filtered by one table.
    "mode":                           (CHAT, COMPOSE),
    "tone calibration":               (CHAT, COMPOSE),
    "approved phrases":               (CHAT, COMPOSE),
    "learned phrases":                (CHAT, COMPOSE),
    "custom instructions":            (CHAT, COMPOSE),
}

# Sections worth sending only when the tools they describe are attached. Once
# the dispatcher narrows the tool list, prose explaining a tool the model
# cannot call is the same dead weight as the schema would have been.
#
# NB: "Editing vs. Adding" is grouped with the calendar tools, not the
# self-edit ones. Despite the name it is entirely about add_calendar_event vs.
# update_calendar_event (AGENTS.md: "adding it again leaves them with
# duplicates") and has nothing to do with Friday editing itself. Coupling it
# to the quip tools would drop the duplicate-event guidance from exactly the
# turns that need it.
_TOOL_COUPLED: dict[str, frozenset[str]] = {
    "calendar writes":        frozenset({"add_calendar_event", "update_calendar_event"}),
    "editing vs. adding":     frozenset({"add_calendar_event", "update_calendar_event"}),
    "calendar title hygiene": frozenset({"add_calendar_event", "update_calendar_event"}),
    "self-editing":           frozenset({"add_quip", "list_quips", "remove_quip",
                                         "update_setting"}),
}

# Where an unrecognized heading goes. Fail OPEN, not closed: AGENTS.md is
# user-editable prose and Friday is an always-on daemon, so an unmapped
# heading must never silently vanish from the persona, and must never be a
# hard startup failure either. It lands everywhere a persona is sent at all
# and logs once so the omission from _MEMBERSHIP is visible.
_DEFAULT = (CHAT, COMPOSE)

_warned: set[str] = set()


def normalize(heading: str) -> str:
    """Heading line → _MEMBERSHIP key. '## Editing vs. Adding' → 'editing vs. adding'."""
    return heading.lstrip("#").strip().lower()


def carries(heading: str, profile: str,
            tool_names: list[str] | None = None) -> bool:
    """True if `profile` should include the section under `heading`.

    tool_names is the dispatcher's selection. Pass None — the default — to
    skip tool-coupled narrowing entirely and get the profile's full slice,
    which is what every non-CHAT caller wants and what CHAT falls back to when
    the dispatcher is disabled.
    """
    key = normalize(heading)
    if tool_names is not None:
        required = _TOOL_COUPLED.get(key)
        if required is not None and not required.intersection(tool_names):
            return False
    profiles = _MEMBERSHIP.get(key)
    if profiles is None:
        if key not in _warned:
            _warned.add(key)
            logger.warning(
                f"Persona section {heading.strip()!r} is not in profiles._MEMBERSHIP "
                f"— defaulting to {'/'.join(_DEFAULT)}. Add it to the table."
            )
        profiles = _DEFAULT
    return profile in profiles


def split_sections(text: str) -> list[tuple[str, str]]:
    """Markdown → [(heading_line, block_text)] in document order.

    A block runs from one ATX heading up to the next, heading line included,
    so "".join(block for _, block in split_sections(t)) == t. Text appearing
    before the first heading is returned first under the "" heading, which is
    how AGENTS.md's opening identity line survives filtering.
    """
    blocks: list[tuple[str, str]] = []
    heading = ""
    buf: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("#"):
            if buf or heading:
                blocks.append((heading, "".join(buf)))
            heading = line
            buf = [line]
        else:
            buf.append(line)
    if buf or heading:
        blocks.append((heading, "".join(buf)))
    return blocks
