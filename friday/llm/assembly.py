"""
llm/assembly.py
Text assembly: LLMRequest -> AssembledPrompt. The dispatcher's half of the
prompt, and the whole of it.

The boundary this file exists to hold:

    TEXT assembly — persona sections, context blocks, history, the user's
    message — happens here, above the providers, once per dispatch.

    SDK CONTENT shaping — turn structure, image parts, schema wiring,
    system-instruction plumbing — happens in the provider.

Before this, render_prompt() lived in providers/base.py and both adapters
imported it. That works right up until the two need to differ, at which point
the natural fix is a provider-local tweak and the two silently start sending
different prompts for the same request. Nothing fails; the answers just get
worse on one provider. Assembling above them makes that class of drift
impossible rather than merely discouraged.

Called only by llm/dispatch.py, and exactly once per dispatch — the same
AssembledPrompt goes to the provider and to the exchange log.
"""

from __future__ import annotations

from llm import persona
from llm.types import AnyTurn, AssembledPrompt, LLMRequest, Profile, Turn
from tools import registry


# The dashboard's Persona page writes four keys. Two of them are readable as
# text and are overlaid onto VOICE below; the other two are not, and naming
# their owner here is cheaper than rediscovering that they are unread:
#
#   persona.custom_instructions — appended to VOICE verbatim.
#   persona.snark_level         — one generated sentence appended to VOICE.
#   persona.preset              — UNREAD. professional | butler | friday is a
#                                 whole-voice swap, not a line of text; it
#                                 wants its own step once there is more than
#                                 one voice to swap between.
#   persona.jarvis_phrases      — UNREAD. A per-phrase approval map, which is
#                                 quip selection, which is step 4 with
#                                 phrases.py. Wiring it into prompt text now
#                                 would put the approval list in every call's
#                                 prefix to no purpose.
_SNARK_LINES = {
    "none": "Drop the sarcasm entirely. Stay warm and plain; wit is not wanted "
            "right now.",
    "medium": "Keep the wit rare — a dry aside only when it genuinely lands, "
              "and never more than once in a reply.",
    "maximum": "The dry wit is welcome. Still one line per reply at most, and "
               "still never at the expense of the answer.",
}


def voice_overlay(config: dict) -> str:
    """Config-driven additions to the VOICE section, or ''.

    Appended AFTER the AGENTS.md text, never merged into it: the file is the
    base and config is the overlay, so a user reading AGENTS.md sees what
    Friday actually starts from and the later line is visibly the override.
    """
    persona_cfg = config.get("persona") or {}
    parts: list[str] = []

    snark = str(persona_cfg.get("snark_level") or "").strip().lower()
    if snark in _SNARK_LINES:
        parts.append(_SNARK_LINES[snark])

    custom = str(persona_cfg.get("custom_instructions") or "").strip()
    if custom:
        # Labeled, because it is the user speaking about how Friday should
        # behave rather than more of Friday's own description of itself.
        parts.append(f"Standing instructions from the user: {custom}")

    return "\n\n".join(parts)


def build_system(request: LLMRequest, profile: Profile,
                 config: dict | None = None) -> str:
    """Persona sections, then labeled context blocks.

    Order is load-bearing. The persona is byte-identical on every call with
    the same profile; the context blocks carry the wall clock and change every
    minute. Persona first keeps the long invariant part at the front, where a
    prefix cache can match it. Prepending the clock would invalidate the whole
    prefix once a minute for no benefit.

    A section AGENTS.md does not currently provide is simply absent —
    llm/persona.py has already warned about it once. A profile requesting no
    sections gets no persona, which is what CLASSIFY-style calls want.
    """
    parts: list[str] = []

    persona_text = persona.assemble(profile.persona_sections)
    if persona_text:
        parts.append(persona_text)

    # Only when the profile actually took VOICE. Overlaying snark onto
    # CLASSIFY would put tone instructions in front of a one-word label.
    if config and "VOICE" in profile.persona_sections:
        overlay = voice_overlay(config)
        if overlay:
            parts.append(overlay)

    for block in request.context_blocks:
        parts.append(f"{block.label}:\n{block.content}")

    return "\n\n".join(parts)


# conversation_history stores exactly these two roles. Anything else is a row
# written by something that predates this code or by a bug; it is dropped
# rather than guessed at, because inventing a role changes who the model
# thinks said what.
_ROLES = ("user", "assistant")


def build_turns(request: LLMRequest) -> tuple[AnyTurn, ...]:
    """History as real turns, with the current message as the final one.

    This replaces the pre-dispatcher wire format, which glued every stored row
    into one "Earlier in this conversation:\nUser: ...\nFriday: ..." blob and
    sent it as a single user turn. That format made the model pay twice: once
    for the role labels themselves, and again for reasoning about a transcript
    of a conversation instead of simply having had it.

    Two properties the flat format could not offer:

      * Consecutive turns are merged. Providers reject or mishandle two
        same-role turns in a row, and a double-write in conversation_history
        (the timeout path writes user+assistant together) can produce them.
      * A leading assistant turn is dropped. The window is a LIMIT over the
        last N rows, so it can begin mid-exchange with Friday's reply, and a
        conversation that opens with the model speaking is not a shape every
        provider accepts.

    Neither is cosmetic: both are cases where the old blob silently worked and
    a real turn list silently would not.
    """
    turns: list[AnyTurn] = []
    for role, content in request.history:
        if role not in _ROLES or not (content or "").strip():
            continue
        # isinstance, not just a role match: only plain text turns may be
        # concatenated. A ToolResultTurn shares the "user" role and has no
        # .text at all, so merging one would be a silent corruption if this
        # loop ever saw one.
        if turns and isinstance(turns[-1], Turn) and turns[-1].role == role:
            turns[-1] = Turn(role=role, text=f"{turns[-1].text}\n\n{content}")
        else:
            turns.append(Turn(role=role, text=content))

    while turns and turns[0].role == "assistant":
        turns.pop(0)

    if turns and turns[-1].role == "user":
        # The window ended on an unanswered user message — fold the new one in
        # rather than emitting two user turns back to back.
        turns[-1] = Turn(role="user", text=f"{turns[-1].text}\n\n{request.prompt}")
    else:
        turns.append(Turn(role="user", text=request.prompt))

    # The within-turn tool exchange goes last, verbatim. Never merged, never
    # reordered, never folded into the text turn before it: this is the
    # transcript of what the model just asked for and what it got back, and
    # the model's next hop is reasoning about that exact sequence.
    turns.extend(request.tool_turns)

    return tuple(turns)


def build_tools(profile: Profile) -> tuple[dict, ...]:
    """The tool declarations this profile may be offered, from the registry.

    Resolved from profile.tool_scope and nothing else. A caller cannot pass
    tools in: there is no field on LLMRequest for them, which is what makes
    "COMPOSE never gets tools" a property of the profile table rather than a
    thing every call site has to remember.

    None scope returns an empty tuple, and the provider turns an empty tuple
    into no `tools` argument at all — not an empty list. Some SDKs treat an
    empty tool list as "tools enabled, none available" and still change how
    they decode; absent is the only unambiguous way to say no.
    """
    return registry.schemas_for_scope(profile.tool_scope)


def assemble(request: LLMRequest, profile: Profile,
             config: dict | None = None) -> AssembledPrompt:
    """The one call. Its result is what the provider sends AND what the
    exchange log records, so the two cannot disagree."""
    return AssembledPrompt(
        system=build_system(request, profile, config),
        turns=build_turns(request),
        tools=build_tools(profile),
    )
