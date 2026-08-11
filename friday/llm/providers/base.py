"""
llm/providers/base.py
The provider contract.

A provider shapes an already-assembled prompt into SDK content and sends it.
It does not assemble: persona, context blocks and history are joined above it
by llm/assembly.py, once per dispatch, so two adapters cannot drift into
sending different prompts for the same request. What is left here is genuinely
per-SDK — turn structure, image parts, schema wiring, system-instruction
plumbing.

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

from llm.types import AssembledPrompt, LLMRequest, LLMResponse, Profile


class Provider(ABC):
    """One model backend. Implementations own their SDK and nothing else."""

    @abstractmethod
    def complete(self, request: LLMRequest, profile: Profile,
                 prompt: AssembledPrompt) -> LLMResponse:
        """Run one call.

        `prompt` is the assembled text — system block and turns — and is the
        only thing that may be sent as content. `request` is still passed for
        what is not text: images, response_schema, the deadline.

        The profile arrives already resolved by the dispatcher rather than
        looked up here: a provider that could resolve a profile name could
        also disagree with the dispatcher about what CHAT means.
        """
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
