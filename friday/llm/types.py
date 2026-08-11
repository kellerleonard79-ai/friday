"""
llm/types.py
The canonical vocabulary for every LLM call in Friday.

Pure data. No behavior, no I/O, and above all no provider SDK import — this
module is what lets the rest of the codebase talk about a model call without
knowing which model answers it. `llm/providers/gemini.py` is the only file
allowed to know that.

Everything here is a frozen dataclass. A request is a value that gets handed
down through the dispatcher to a provider; nothing in that chain may mutate it
in place, because the dispatcher retries by re-sending the same object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Why the call ended. "tool_calls" and "length" are unused until the tool layer
# lands (step 3) — they are here so a provider never has to invent a value.
Finish = Literal["stop", "tool_calls", "length", "error"]

# What KIND of failure, when finish == "error". This distinction is the whole
# point of the enum and must not collapse into a generic exception:
#   rate_limit — the API answered and refused us (429, quota). Retryable, on
#                the longer backoff: quota does not clear in a second.
#   transient  — the API answered with a server fault (500, 502, 503, 504).
#                Retryable, on the shorter backoff: nothing is exhausted, the
#                far side is having a bad minute and the redial usually works.
#   network    — we never reached the API (DNS, refused, timeout, dead socket).
#                NOT retryable here; a blocked network must fail fast and say so.
#   fatal      — anything else (bad request, auth, malformed response). Ours.
# transient is deliberately NOT folded into rate_limit even though both retry:
# "Google is having a bad day", "you are over quota" and "this network blocks
# the API" are three different problems with three different answers, and the
# dashboard and the reachability module both have to tell them apart.
ErrorKind = Literal["none", "rate_limit", "transient", "network", "fatal"]


@dataclass(frozen=True, slots=True)
class Profile:
    """A named calling convention: which model, how much of the persona, which
    tools, and what it may spend. Only CHAT exists in step 1.

    persona_sections and tool_scope are declared but inert — the persona layer
    lands in step 2 and the tool layer in step 3. They are in the type now so
    those steps add a table entry rather than a field to every call site.
    """
    name: str
    model: str
    persona_sections: tuple[str, ...] = ()
    tool_scope: None = None
    max_output_tokens: int = 1024
    temperature: float = 0.7
    timeout_s: float = 60.0
    max_tool_hops: int = 0


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """A labeled slab of deterministic context (schedule, weather, events).
    The label is rendered into the prompt, so it is part of what the model
    sees — not a debugging name."""
    label: str
    content: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One call's worth of intent. Assembled by the caller, resolved by the
    dispatcher, rendered by the provider.

    profile     — carries the caller's copy; the dispatcher re-resolves it by
                  name against the registry and uses THAT, so a hand-rolled
                  Profile cannot smuggle in a different model or token cap.
    history     — (role, content) oldest-first. Rendering is the provider's job.
    images      — (bytes, mime_type). The media path is offline until the
                  EXTRACT profile lands, but the field exists so there is never
                  a reason to open a second door to the model for images.
    deadline    — time.monotonic() value past which nothing may still be tried.
                  Set by the dispatcher at entry; None means "not yet resolved"
                  and only ever appears before dispatch.
    """
    profile: Profile
    prompt: str
    context_blocks: tuple[ContextBlock, ...] = ()
    history: tuple[tuple[str, str], ...] = ()
    response_schema: Any | None = None
    triggered_by: str = "unknown"
    images: tuple[tuple[bytes, str], ...] = ()
    deadline: float | None = None


@dataclass(frozen=True, slots=True)
class Turn:
    """One conversational turn, in Friday's vocabulary rather than any SDK's.

    role is "user" or "assistant" because that is what conversation_history
    stores. Gemini calls the second one "model"; mapping to that name is the
    Gemini adapter's job and must not leak up here.
    """
    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True, slots=True)
class AssembledPrompt:
    """Everything textual a provider needs, already assembled.

    Built once per dispatch by llm/assembly.py and handed to the provider,
    which shapes it into SDK content and concatenates nothing of its own. Two
    providers that each did their own assembly would drift, and the drift
    would be invisible: both would keep answering.

    system — persona sections then context blocks, in that order. Persona
             first on purpose: it is identical call to call, so it stays a
             stable cacheable prefix in front of the clock, which is not.
    turns  — the whole conversation including the current user message as the
             final turn. There is no separate "prompt" field, because a
             provider that received both would have to decide how to join
             them, which is the assembly this type exists to have already done.
    """
    system: str = ""
    turns: tuple[Turn, ...] = ()

    def for_log(self) -> str:
        """The exact assembled call, serialized for llm_exchanges.full_prompt.

        Rendered from the same object the provider was handed, never
        re-derived from the request: a log that can disagree with what was
        sent is worse than no log, because it will be trusted.
        """
        parts = []
        if self.system:
            parts.append(f"[system]\n{self.system}")
        parts.extend(f"[{t.role}]\n{t.text}" for t in self.turns)
        return "\n\n".join(parts)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A function call the model asked for. Defined now, unused until step 3."""
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Usage:
    """What the call cost. Zeros on a failed call — a call that never reached
    the API has no usage metadata, and the row is marked by error_kind anyway."""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: str = ""


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """What came back. Never an exception for an API-level failure: a provider
    that cannot reach the model returns finish="error" with an error_kind.

    error_message carries the first line of the underlying failure. It exists
    because the user-facing "LLM error, sir: …" reply needs something to show
    for a fatal, and because the alternative is the channel reaching into the
    provider for it — which is the cross-layer reach this architecture exists
    to remove.
    """
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)
    finish: Finish = "stop"
    error_kind: ErrorKind = "none"
    error_message: str = ""
