"""
llm/profiles.py
The profile registry: which model a given kind of call uses, how much of the
persona it carries, and what it may spend.

A profile is a *calling convention*, not a model. "CLASSIFY is terse, cheap and
deterministic" is the durable statement; which concrete model implements that
is a config question and moves over time. So the table below is split in two:

  _SPECS  — the code half. Persona sections, tool scope, timeout, hop budget.
            These are architecture. Config may not override them, because a
            config file that could hand CLASSIFY the tool layer or give CHAT a
            600s deadline is a config file that can break invariants.
  config  — the money half. Model, output cap, temperature. These are tuning
            knobs the dashboard and the user legitimately own.

The registry is authoritative. llm/dispatch.py re-resolves every request's
profile by name against this table and calls with the result, so a caller
cannot hand-roll a Profile named CHAT that quietly uses a different model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from llm import persona
from llm.types import Profile

logger = logging.getLogger("friday.llm.profiles")


@dataclass(frozen=True, slots=True)
class _Spec:
    """The half of a profile that config may not touch."""
    timeout_s: float
    persona_sections: tuple[str, ...] = ()
    tool_scope: tuple[str, ...] | None = None
    max_tool_hops: int = 0
    # Used only when config names no value for this profile.
    default_max_output_tokens: int = 1000
    default_temperature: float = 1.0


# CHAT's timeout is the whole budget for one dispatch — the model call, the
# single transport redial, and every retry come out of it. It sits under
# channels/telegram.py::_EXECUTOR_TIMEOUT_S (150) so the deadline expires
# first and the pipeline is released by design rather than by the backstop.
#
_SPECS: dict[str, _Spec] = {
    "CHAT": _Spec(
        timeout_s=120.0,
        # Not URGENCY — that is the connector work's, for tagging ingested rows.
        # TOOL_POLICY joins in step 3, rewritten down to the two read-only
        # tools that now exist: as written for Phase II it was 995 tokens
        # describing add_calendar_event, update_calendar_event, add_quip and
        # update_setting, none of which survive. The old text is kept in
        # AGENTS.md under DEFERRED, which no profile can address.
        persona_sections=("IDENTITY", "TIME", "VOICE", "FORMATTING", "TOOL_POLICY"),
        tool_scope=("read",),
        # Three rounds of tool execution, so four model calls at worst. Sized
        # against the deadline and the calendar, not picked round: the JXA
        # reader costs ~30s and the per-turn cache makes only the first read
        # expensive, so three hops fit inside 120s with room for the answer.
        # Raising this without granting the daemon EventKit access is how a
        # turn starts timing out mid-conversation.
        max_tool_hops=3,
        # 1.0 is what every chat call ran at before the dispatcher. Named
        # explicitly so it is a decision rather than an SDK default that can
        # move under us, and left at the old value so a tone change reads as
        # the persona landing rather than as a temperature edit.
        default_temperature=1.0,
    ),

    # Briefings. Same model as CHAT: a briefing is the most-read thing Friday
    # writes and there is nothing to save by making it worse.
    #
    # NOT VOICE. Invariant 6 — persona voice applies only to conversational
    # replies, never to cards, briefings, errors or TTS. Handing the butler
    # wit to the profile that writes briefings is the direct route to a joke
    # in the middle of a schedule. FORMATTING stays: it is the section that
    # governs markdown and structure, which a briefing very much needs.
    "COMPOSE": _Spec(
        timeout_s=90.0,
        persona_sections=("IDENTITY", "TIME", "FORMATTING"),
        tool_scope=None,
        max_tool_hops=0,
        default_max_output_tokens=2048,
        default_temperature=1.0,
    ),

    # Urgency tagging and announcement filtering: reads a record, returns a
    # label. TIME because "due tomorrow" is only urgent relative to a date.
    #
    # NOT IDENTITY, and that is a correction rather than a saving: IDENTITY
    # carries the sourcing rule ("state where this came from"), which was
    # bleeding cited prose into what has to be a bare label. Also not VOICE or
    # FORMATTING — ~2500 characters of butler wit and markdown policy that
    # cannot improve a one-word answer, on a profile that runs once per
    # ingested row rather than once per user message.
    #
    # temperature 0.0: two runs over the same announcement disagreeing about
    # URGENT is a worse failure than either answer.
    "CLASSIFY": _Spec(
        timeout_s=45.0,
        persona_sections=("TIME",),
        tool_scope=None,
        max_tool_hops=0,
        default_max_output_tokens=64,
        default_temperature=0.0,
    ),
}

# A section name outside persona.SECTIONS is a typo in this file, not a user
# edit, so it fails here at import — before the daemon serves anything and
# regardless of which profile happens to run first. The other direction (a
# vocabulary section AGENTS.md does not currently provide) only warns; see
# llm/persona.py for why the two are asymmetric.
persona.validate(s for spec in _SPECS.values() for s in spec.persona_sections)

_registry: dict[str, Profile] = {}


def build(config: dict) -> dict[str, Profile]:
    """Construct the profile table from config. Pure — returns, installs nothing.

    Per profile, each of model / max_output_tokens / temperature resolves as:
        profiles.<NAME>.<key>  →  the provider block  →  the spec default

    The provider-block step is the compatibility layer: a config written before
    profiles existed carries one `<provider>.model` and one `<provider>.max_tokens`
    for the whole process, and must still boot.
    """
    provider = config.get("provider", "ollama")
    provider_cfg = config.get(provider, {}) or {}
    profiles_cfg = config.get("profiles") or {}

    fallback_model = provider_cfg.get("model", "")
    legacy_max_tokens = provider_cfg.get("max_tokens")
    legacy_used: list[str] = []

    table: dict[str, Profile] = {}
    for name, spec in _SPECS.items():
        entry = profiles_cfg.get(name) or {}

        model = entry.get("model") or fallback_model
        if not model:
            raise ValueError(
                f"No model configured for profile {name}. Set profiles.{name}.model "
                f"or {provider}.model."
            )

        max_output_tokens = entry.get("max_output_tokens")
        if max_output_tokens is None:
            if legacy_max_tokens is not None:
                max_output_tokens = legacy_max_tokens
                legacy_used.append(name)
            else:
                max_output_tokens = spec.default_max_output_tokens

        temperature = entry.get("temperature")
        if temperature is None:
            temperature = spec.default_temperature

        table[name] = Profile(
            name=name,
            model=str(model),
            persona_sections=spec.persona_sections,
            tool_scope=spec.tool_scope,
            max_output_tokens=int(max_output_tokens),
            temperature=float(temperature),
            timeout_s=spec.timeout_s,
            max_tool_hops=spec.max_tool_hops,
        )

    if legacy_used:
        # One line per build, not per profile: this is a config nudge, not an
        # error, and a daemon logs it at every boot until someone acts on it.
        logger.warning(
            f"{provider}.max_tokens is deprecated and applied to "
            f"{', '.join(sorted(legacy_used))}. A single global cap makes "
            f"CLASSIFY pay CHAT's ceiling — move it to "
            f"profiles.<NAME>.max_output_tokens."
        )

    return table


def install(config: dict) -> None:
    """Build and install the registry. Called once at startup."""
    global _registry
    _registry = build(config)
    logger.info(
        "Profiles installed: "
        + ", ".join(
            f"{p.name}({p.model}, {p.max_output_tokens}tok, t={p.temperature})"
            for p in _registry.values()
        )
    )


def get(name: str) -> Profile:
    """Look up a profile by name. Raises on an unknown name — a typo'd profile
    is a bug, and falling back to CHAT would hide it behind a bigger bill."""
    try:
        return _registry[name]
    except KeyError:
        known = ", ".join(sorted(_registry)) or "<none installed>"
        raise KeyError(f"Unknown LLM profile {name!r}. Known: {known}") from None


def names() -> tuple[str, ...]:
    """Every profile in the code's table, installed or not. The persona module
    uses this to validate section names across the whole table at import."""
    return tuple(_SPECS)
