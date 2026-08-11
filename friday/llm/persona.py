"""
llm/persona.py
AGENTS.md, parsed into named sections a profile can address.

The file is user-editable prose that a daemon reads. Those two facts set every
rule here:

  * Editing AGENTS.md takes effect on the next call, with no restart. The
    parse is cached on mtime, not on process lifetime.
  * A section a profile asks for but the file does not provide is a WARNING and
    an omission, never an exception. A renamed heading must not stop an
    always-on secretary from answering.
  * A section name outside SECTIONS is a Python typo — a profile naming one
    raises at validation time, before the daemon serves anything.

The asymmetry is deliberate. The first case is the user editing prose; the
second is a developer misspelling a constant. Only one of them should be able
to take Friday off the air.

No SDK import, no config read, no I/O beyond the one file. Assembly order and
what is done with the text belong to llm/dispatch.py.
"""

from __future__ import annotations

import logging
import re
import threading

import paths

logger = logging.getLogger("friday.llm.persona")

# The vocabulary. A heading in AGENTS.md outside this set is parsed and
# ignored; a name outside it in a profile raises. Order is the assembly order
# when a profile requests several — identity before rules before style, which
# is the order the sections were written to be read in.
SECTIONS: tuple[str, ...] = (
    "IDENTITY",
    "TIME",
    "URGENCY",
    "VOICE",
    "FORMATTING",
    "TOOL_POLICY",
)

_PERSONA_FILE = "AGENTS.md"

# Level-2 ATX headings only. The H3s inside a section are body text — they are
# the original organization of the prose and belong to whatever section
# contains them.
_H2 = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)

_lock = threading.Lock()
_cache: dict = {"mtime": None, "sections": {}}


def _parse(text: str) -> dict[str, str]:
    """Split on '## HEADING' into {NAME: body}. Anything before the first
    heading is dropped — that is the file's own preamble comment, not persona."""
    sections: dict[str, str] = {}
    matches = list(_H2.finditer(text))
    for i, m in enumerate(matches):
        name = m.group(1).strip().upper()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        if body:
            sections[name] = body
    return sections


def _load() -> dict[str, str]:
    """The parsed file, re-read when its mtime changes.

    Warnings about unknown headings are emitted here — once per parse rather
    than once per request, so a daemon logs a renamed heading when it happens
    instead of on every message for the rest of the day.
    """
    path = paths.resource_path(_PERSONA_FILE)
    try:
        mtime = path.stat().st_mtime
    except OSError as e:
        # Missing AGENTS.md is survivable: Friday answers without a persona
        # rather than not answering. Logged loudly because it is never normal.
        with _lock:
            if _cache["mtime"] != "missing":
                logger.error(f"{_PERSONA_FILE} unreadable ({e}) — no persona will be applied.")
                _cache["mtime"] = "missing"
                _cache["sections"] = {}
            return _cache["sections"]

    with _lock:
        if _cache["mtime"] == mtime:
            return _cache["sections"]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.error(f"{_PERSONA_FILE} read failed ({e}) — keeping the last good parse.")
        with _lock:
            return _cache["sections"]

    sections = _parse(text)

    unknown = sorted(set(sections) - set(SECTIONS))
    if unknown:
        logger.warning(
            f"{_PERSONA_FILE} has heading(s) no profile can address: "
            f"{', '.join(unknown)}. Known: {', '.join(SECTIONS)}."
        )
    missing = [s for s in SECTIONS if s not in sections]
    if missing:
        logger.warning(
            f"{_PERSONA_FILE} is missing section(s): {', '.join(missing)}. "
            f"Profiles requesting them get nothing."
        )

    with _lock:
        _cache["mtime"] = mtime
        _cache["sections"] = sections
    logger.info(
        f"{_PERSONA_FILE} parsed: {len(sections)} section(s) — "
        + ", ".join(sorted(sections))
    )
    return sections


def validate(names) -> None:
    """Raise if any name is outside SECTIONS.

    Called once at startup against every profile in the table, so a typo'd
    section name fails at boot rather than the first time that profile is
    used — which for CLASSIFY could be hours later, on a background job, with
    the persona silently absent the whole time.
    """
    bad = sorted(set(names) - set(SECTIONS))
    if bad:
        raise ValueError(
            f"Unknown persona section(s): {', '.join(bad)}. "
            f"Known: {', '.join(SECTIONS)}. Fix the profile, or add the "
            f"section to llm/persona.py::SECTIONS and to {_PERSONA_FILE}."
        )


def get(name: str) -> str:
    """One section's body, or '' if AGENTS.md does not currently provide it."""
    return _load().get(name, "")


def assemble(names) -> str:
    """The requested sections, in SECTIONS order, as one block of text.

    Order comes from SECTIONS rather than from the caller's tuple so that two
    profiles listing the same sections differently still produce a
    byte-identical prefix — which is what makes the persona cacheable.

    Sections AGENTS.md does not provide are skipped silently; _load() has
    already warned about them once.
    """
    sections = _load()
    wanted = set(names)
    return "\n\n".join(
        sections[n] for n in SECTIONS if n in wanted and sections.get(n)
    )


def sections_present() -> tuple[str, ...]:
    """Which vocabulary sections the file currently provides. For diagnostics."""
    sections = _load()
    return tuple(n for n in SECTIONS if n in sections)
