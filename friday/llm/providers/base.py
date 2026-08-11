"""
llm/providers/base.py
The provider contract, and the prompt assembly every provider shares.

A Provider takes an LLMRequest and returns an LLMResponse. It never raises for
an API-level failure — a refused, unreachable or malformed call comes back as
finish="error" with an error_kind, because the dispatcher's retry policy is
driven by that enum and an exception carries no policy. Programming errors
(a bad argument, a missing attribute) may still raise; those are bugs, not
outcomes.

A Provider never retries a policy failure either. One narrow transport redial
is allowed — see gemini.py — because a dead socket is a transport concern, not
a policy one. Everything else is the dispatcher's call.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from llm.types import LLMRequest, LLMResponse, Profile


class Provider(ABC):
    """One model backend. Implementations own their SDK and nothing else."""

    @abstractmethod
    def complete(self, request: LLMRequest, profile: Profile) -> LLMResponse:
        """Run one call. The profile is passed in already resolved by the
        dispatcher rather than looked up here: a provider that could resolve a
        profile name could also disagree with the dispatcher about what CHAT
        means."""
        raise NotImplementedError


def remaining_seconds(request: LLMRequest) -> float:
    """Seconds left before the request's deadline. Negative or zero means the
    budget is spent and nothing further may be attempted.

    A request with no deadline has not been through the dispatcher. That is a
    programming error, not a runtime condition, so it raises.
    """
    if request.deadline is None:
        raise ValueError("LLMRequest.deadline is unset — call through llm.dispatch")
    return request.deadline - time.monotonic()


def render_prompt(request: LLMRequest) -> str:
    """context blocks (labeled) -> history -> prompt, as one flat string.

    Shared so two providers cannot drift into sending different prompts for the
    same request.

    The history format is inherited verbatim from the throwaway _with_history()
    in channels/telegram.py that this replaces — flat labeled lines, no role
    structure. It is deliberately unchanged in step 1 so that wiring the
    dispatcher in is not also a change to what the model reads. Real multi-turn
    history is a step-2 decision.
    """
    parts: list[str] = []

    # ── PERSONA ASSEMBLY POINT ────────────────────────────────────────────
    # Step 2 renders profile.persona_sections here, ahead of the context
    # blocks, as a stable cacheable prefix. Deliberately not implemented:
    # there is no section lookup in step 1 and persona_sections is always ().
    # ──────────────────────────────────────────────────────────────────────

    for block in request.context_blocks:
        parts.append(f"{block.label}:\n{block.content}")

    if request.history:
        lines = [
            f"{'User' if role == 'user' else 'Friday'}: {content}"
            for role, content in request.history
        ]
        parts.append("Earlier in this conversation:\n" + "\n".join(lines))
        parts.append(f"User: {request.prompt}")
    else:
        parts.append(request.prompt)

    return "\n\n".join(p for p in parts if p)
