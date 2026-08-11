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
from llm.types import AssembledPrompt, LLMRequest, Profile, Turn


def build_system(request: LLMRequest, profile: Profile) -> str:
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

    for block in request.context_blocks:
        parts.append(f"{block.label}:\n{block.content}")

    return "\n\n".join(parts)


def build_turns(request: LLMRequest) -> tuple[Turn, ...]:
    """History plus the current message, as one flat user turn.

    NOTE: this deliberately reproduces the pre-dispatcher wire format
    byte-for-byte — history glued into an "Earlier in this conversation:"
    preamble ahead of the real message, inside a single user turn. It is
    preserved here only so that moving assembly up the stack is not also a
    change to what the model reads; de-flattening it is the next commit and is
    measured on its own.
    """
    if not request.history:
        return (Turn(role="user", text=request.prompt),)

    lines = [
        f"{'User' if role == 'user' else 'Friday'}: {content}"
        for role, content in request.history
    ]
    text = (
        "Earlier in this conversation:\n"
        + "\n".join(lines)
        + f"\n\nUser: {request.prompt}"
    )
    return (Turn(role="user", text=text),)


def assemble(request: LLMRequest, profile: Profile) -> AssembledPrompt:
    """The one call. Its result is what the provider sends AND what the
    exchange log records, so the two cannot disagree."""
    return AssembledPrompt(
        system=build_system(request, profile),
        turns=build_turns(request),
    )
