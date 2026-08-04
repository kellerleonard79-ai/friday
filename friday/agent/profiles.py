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

ALL = (CHAT, COMPOSE, CLASSIFY)

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


def carries(heading: str, profile: str) -> bool:
    """True if `profile` should include the section under `heading`."""
    key = normalize(heading)
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
