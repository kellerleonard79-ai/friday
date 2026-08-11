"""
phrases.py
Which of Friday's canned lines to say, and when not to say one.

SILENCE BEATS THE WRONG VIBE. A quip appended to an outcome it contradicts —
"touch grass" on an outdoor event, a jaunty line on a failure — reads worse
than no quip at all. So quips are grouped by TOOL AND OUTCOME (quips.yaml),
and a group with nothing in it produces the empty string. Nothing borrows from
a neighbouring group to avoid being empty.

NO MODEL CALL. Phase II spent a second model call per auto-write turn asking
which quip fit; grouping the palette by outcome buys the same thing for free.
Selection is random.choice over the group, with a short memory so the same
line does not come back twice running.

Reloaded from disk on every call, as before: a quip added over Telegram is in
play on the very next action, with no restart.
"""

import random
from collections import defaultdict, deque

import yaml

import paths
import self_edit

_QUIPS_FILE = str(paths.resource_path("quips.yaml"))

# How many recent picks to avoid per group. Five is chosen against the group
# sizes: with six quips it guarantees a full rotation before a repeat, and with
# two it degenerates to strict alternation, which is the best available.
_NO_REPEAT = 5
_recent: dict[str, deque] = defaultdict(lambda: deque(maxlen=_NO_REPEAT))


def _load_file() -> dict:
    try:
        with open(_QUIPS_FILE) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def bundled_quips() -> list[str]:
    """Every quip that ships with the app, flattened.

    A FLAT VIEW OF A GROUPED FILE, and its two consumers both want exactly
    that: self_edit.py dedups a newly taught quip against it, and the
    dashboard lists it so a bundled line can be retired. Neither cares which
    group a quip belongs to; both would be wrong if the list omitted most of
    the palette.

    Nothing SELECTS from this — selection goes through quip_for(), which is
    group-aware. Read-only: quips.yaml resolves into the PyInstaller bundle on
    frozen builds, so anything learned at runtime goes to friday_voice.yaml.
    """
    data = _load_file()
    out: list[str] = [str(q) for q in (data.get("confirm_quips") or [])]
    for groups in (data.get("tools") or {}).values():
        if not isinstance(groups, dict):
            continue
        for quips in groups.values():
            out.extend(str(q) for q in (quips or []))
    seen: set[str] = set()
    return [q for q in out
            if not (q.casefold() in seen or seen.add(q.casefold()))]


def quip_groups() -> dict[str, dict[str, list[str]]]:
    """The palette as it actually is: tool -> outcome -> quips. For the
    dashboard, so a group with nothing in it is visible as a deliberate
    silence rather than looking like a missing feature."""
    data = _load_file().get("tools") or {}
    return {
        tool: {outcome: [str(q) for q in (quips or [])]
               for outcome, quips in (groups or {}).items()}
        for tool, groups in data.items() if isinstance(groups, dict)
    }


def _group(tool: str, outcome: str) -> tuple[list[str], bool]:
    """(the group's quips, whether the tool has any groups at all).

    The second value is what tells an empty group apart from an unknown tool.
    An empty group is a deliberate "say nothing here" — `failure: []` is a
    decision, not an omission — and an unknown tool is a caller bug. Both are
    silence, but only one of them may draw on the learned pool.
    """
    tools = _load_file().get("tools") or {}
    entry = tools.get(tool)
    if not isinstance(entry, dict):
        return [], False
    return [str(q) for q in (entry.get(outcome) or [])], True


def _pool(tool: str, outcome: str) -> list[str]:
    """Everything sayable for this tool and outcome, minus what is retired.

    Learned quips join a known tool's SUCCESS group only. A line the user
    taught Friday while approving a calendar event is a confirmation quip;
    attaching it to a failure is the same contradiction this module exists to
    avoid, and attaching it to a tool that has no groups at all means a quip
    ships wherever a key was typo'd.

    The BUNDLED flat list is deliberately not folded in. It is legacy — the
    one-list-for-every-occasion palette that put "touch grass" on an outdoor
    event — and it stays readable by the dashboard without being sayable by
    anything. New bundled quips belong in a group.
    """
    store = self_edit.load()
    disabled = {q.casefold() for q in store["disabled_quips"]}
    group, known = _group(tool, outcome)
    pool = list(group)
    if known and outcome == "success":
        pool += [q for q in store["confirm_quips"]]
    seen: set[str] = set()
    out = []
    for q in pool:
        key = q.casefold()
        if key in disabled or key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def quip_for(tool: str, outcome: str) -> str:
    """One quip for this tool and outcome, or "" when there is nothing apt.

    The empty string is a real answer and callers must render it as no quip
    rather than as a missing one — see the module docstring.
    """
    pool = _pool(tool, outcome)
    if not pool:
        return ""
    key = f"{tool}:{outcome}"
    recent = _recent[key]
    fresh = [q for q in pool if q not in recent]
    # Everything has been said recently: fall back to the whole pool rather
    # than to silence. The memory is there to stop a repeat, not to run out.
    choice = random.choice(fresh or pool)
    recent.append(choice)
    return choice


def quip_for_key(quip_key: str) -> str:
    """`"<tool>:<outcome>"`, as carried on a SendMessage effect. An unparseable
    or unknown key is silence, never a fallback quip — a key that does not
    resolve is a bug, and answering it with a random line is how a wrong quip
    ships without anybody noticing."""
    if not quip_key or ":" not in quip_key:
        return ""
    tool, _, outcome = quip_key.partition(":")
    return quip_for(tool, outcome)
