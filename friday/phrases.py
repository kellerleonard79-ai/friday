import random

import yaml

import paths
import self_edit

_QUIPS_FILE = str(paths.resource_path("quips.yaml"))

_FALLBACK = "As you wish, sir."


def bundled_quips() -> list[str]:
    """The quips that ship with the app. Read-only — quips.yaml resolves into
    the PyInstaller bundle on frozen builds. Anything Friday learns at runtime
    lives in self_edit's voice file instead."""
    try:
        with open(_QUIPS_FILE) as f:
            data = yaml.safe_load(f)
        return [str(q) for q in (data.get("confirm_quips") or [])]
    except Exception:
        return []


def _load_quips() -> list[str]:
    """Bundled quips plus anything the user has taught Friday, minus anything
    they have retired. Re-read from disk on every call — a quip added over
    Telegram is in play on the very next action, with no restart."""
    store = self_edit.load()
    disabled = {q.casefold() for q in store["disabled_quips"]}
    quips = [q for q in bundled_quips() if q.casefold() not in disabled]
    seen = {q.casefold() for q in quips}
    for q in store["confirm_quips"]:
        if q.casefold() not in seen:
            quips.append(q)
            seen.add(q.casefold())
    return quips or [_FALLBACK]


def random_quip(context: str = "") -> str:
    """One quip, chosen at random. Reloaded from disk every call — a quip added
    over Telegram is in play on the very next action, with no restart.

    `context` describes the event the quip will be appended to. It is accepted
    and ignored: the LLM-driven selection that used to read it was torn down
    with the rest of the prompt layer. Keeping the parameter means the call
    sites already pass what a context-aware selector will need, and it marks
    the places where a contradictory quip ("touch grass" on an outdoor event)
    can currently ship — that is a known regression of the teardown, not an
    oversight.
    """
    quips = _load_quips()
    return random.choice(quips) if quips else _FALLBACK
