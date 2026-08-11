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


# conversation_history stores exactly these two roles. Anything else is a row
# written by something that predates this code or by a bug; it is dropped
# rather than guessed at, because inventing a role changes who the model
# thinks said what.
_ROLES = ("user", "assistant")


def build_turns(request: LLMRequest) -> tuple[Turn, ...]:
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
    turns: list[Turn] = []
    for role, content in request.history:
        if role not in _ROLES or not (content or "").strip():
            continue
        if turns and turns[-1].role == role:
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

    return tuple(turns)


def assemble(request: LLMRequest, profile: Profile) -> AssembledPrompt:
    """The one call. Its result is what the provider sends AND what the
    exchange log records, so the two cannot disagree."""
    return AssembledPrompt(
        system=build_system(request, profile),
        turns=build_turns(request),
    )
