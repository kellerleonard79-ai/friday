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


def random_quip() -> str:
    quips = _load_quips()
    if quips:
        return random.choice(quips)
    return _FALLBACK


def quip_prompt(context: str) -> tuple[str, list[str]]:
    """
    Returns (prompt_string, quips_list).
    Caller passes prompt to _think(), gets back an integer string,
    then calls pick_quip(response, quips_list) to resolve it.
    """
    quips = _load_quips()
    numbered = "\n".join(f"{i+1}. {q}" for i, q in enumerate(quips))
    prompt = (
        f"{context}\n\n"
        f"Pick the quip that best fits this specific event. "
        f"Avoid contradictions — e.g. a 'touch grass' quip is wrong for an "
        f"outdoor event, a 'sleep schedule' or 'all-nighter' quip is wrong "
        f"for a daytime event, an 'academic' quip is wrong for an errand. "
        f"Reply with a single integer and nothing else.\n\n"
        f"{numbered}"
    )
    return prompt, quips


def pick_quip(response: str, quips: list[str]) -> str:
    """Resolves the model's integer response to a quip string."""
    try:
        idx = int(response.strip()) - 1
        if 0 <= idx < len(quips):
            return quips[idx]
    except (ValueError, IndexError):
        pass
    return random.choice(quips) if quips else _FALLBACK
