import random
import yaml

import paths

_QUIPS_FILE = str(paths.resource_path("quips.yaml"))


def _load_quips() -> list[str]:
    try:
        with open(_QUIPS_FILE) as f:
            data = yaml.safe_load(f)
        return data.get("confirm_quips", [])
    except Exception:
        return ["As you wish, sir."]


def random_quip() -> str:
    quips = _load_quips()
    if quips:
        return random.choice(quips)
    return "As you wish, sir."


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
    return random.choice(quips)
