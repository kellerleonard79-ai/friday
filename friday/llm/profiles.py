"""
llm/profiles.py
The profile registry: which model a given kind of call uses, and what it may spend.

Only CHAT exists in step 1. CLASSIFY, COMPOSE and EXTRACT arrive in step 2 with
the persona layer — defining them now would be four table entries nothing calls
and three persona_sections tuples nobody can fill.

The registry is authoritative. llm/dispatch.py re-resolves every request's
profile by name against this table and calls with the result, so a caller
cannot hand-roll a Profile named CHAT that quietly uses a different model.
"""

from __future__ import annotations

import logging

from llm.types import Profile

logger = logging.getLogger("friday.llm.profiles")

# Config has no temperature or timeout key today, so these are constants.
#
# _CHAT_TIMEOUT_S is the whole budget for one dispatch — the model call, the
# single transport redial, and any rate-limit retries all come out of it. It
# sits under channels/telegram.py::_EXECUTOR_TIMEOUT_S (150) so the deadline
# expires first and the pipeline is released by design rather than by the
# channel's backstop.
_CHAT_TIMEOUT_S = 120.0

# Gemini's own default is higher; chat wants steadier phrasing than that.
_CHAT_TEMPERATURE = 0.7

_DEFAULT_MAX_OUTPUT_TOKENS = 1000

_registry: dict[str, Profile] = {}


def build(config: dict) -> dict[str, Profile]:
    """Construct the profile table from config. Pure — returns, installs nothing."""
    provider = config.get("provider", "ollama")
    provider_cfg = config.get(provider, {}) or {}
    model = provider_cfg.get("model", "")
    if not model:
        raise ValueError(f"No model configured for provider {provider!r}")

    # TODO step 2: gemini.max_tokens is a single global cap today. Once there
    # are four profiles it has to become a per-profile map — CLASSIFY returning
    # one label must not carry CHAT's ceiling.
    max_tokens = int(provider_cfg.get("max_tokens", _DEFAULT_MAX_OUTPUT_TOKENS))

    return {
        "CHAT": Profile(
            name="CHAT",
            model=model,
            persona_sections=(),   # step 2
            tool_scope=None,       # step 3
            max_output_tokens=max_tokens,
            temperature=_CHAT_TEMPERATURE,
            timeout_s=_CHAT_TIMEOUT_S,
            max_tool_hops=0,
        ),
    }


def install(config: dict) -> None:
    """Build and install the registry. Called once at startup."""
    global _registry
    _registry = build(config)
    logger.info(
        "Profiles installed: "
        + ", ".join(f"{p.name}({p.model})" for p in _registry.values())
    )


def get(name: str) -> Profile:
    """Look up a profile by name. Raises on an unknown name — a typo'd profile
    is a bug, and falling back to CHAT would hide it behind a bigger bill."""
    try:
        return _registry[name]
    except KeyError:
        known = ", ".join(sorted(_registry)) or "<none installed>"
        raise KeyError(f"Unknown LLM profile {name!r}. Known: {known}") from None
