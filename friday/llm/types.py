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

import json
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

    tool_scope is the set of scope tags whose tools this profile may see.
    None means NO TOOLS, and that is the guarantee — not "no filter". A profile
    that never opted in cannot be handed tools by a caller, a config edit, or a
    refactor that forgets to pass a scope: the provider is never given a tools
    argument at all.

    The annotation was literally `None` in step 1, which made the guarantee a
    type error. Widening it to a tuple weakens that, so __post_init__ enforces
    what the annotation no longer can — including rejecting the empty tuple,
    which would silently mean "no tools" while reading like it means something.
    """
    name: str
    model: str
    persona_sections: tuple[str, ...] = ()
    tool_scope: tuple[str, ...] | None = None
    max_output_tokens: int = 1024
    temperature: float = 0.7
    timeout_s: float = 60.0
    max_tool_hops: int = 0

    def __post_init__(self) -> None:
        if self.tool_scope is None:
            return
        if not isinstance(self.tool_scope, tuple) or not self.tool_scope:
            raise ValueError(
                f"Profile {self.name}: tool_scope must be None (no tools) or a "
                f"non-empty tuple of scope tags, got {self.tool_scope!r}. An "
                f"empty tuple means no tools while looking like it means "
                f"something — say None."
            )
        if self.max_tool_hops < 1:
            raise ValueError(
                f"Profile {self.name}: tool_scope={self.tool_scope} with "
                f"max_tool_hops={self.max_tool_hops}. A profile offered tools "
                f"it can never call would burn the schema tokens on every "
                f"request and never use one."
            )


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
    tool_turns  — the within-turn tool exchange so far: what the model asked
                  for and what came back, appended AFTER the current user
                  message. Distinct from `history`, which is the persisted
                  conversation, because these are not persisted: a tool call
                  is scaffolding for one answer, and replaying it into the
                  next user message would have the model re-reading a stale
                  calendar as though it were fresh.
    """
    profile: Profile
    prompt: str
    context_blocks: tuple[ContextBlock, ...] = ()
    history: tuple[tuple[str, str], ...] = ()
    response_schema: Any | None = None
    triggered_by: str = "unknown"
    images: tuple[tuple[bytes, str], ...] = ()
    deadline: float | None = None
    tool_turns: tuple[AnyTurn, ...] = ()


@dataclass(frozen=True, slots=True)
class Turn:
    """One conversational turn of plain text, in Friday's vocabulary rather
    than any SDK's.

    role is "user" or "assistant" because that is what conversation_history
    stores. Gemini calls the second one "model"; mapping to that name is the
    Gemini adapter's job and must not leak up here.
    """
    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallTurn:
    """The assistant asking for a tool, as a turn in its own right.

    Always the assistant's — a tool call is something the model emitted, and
    replaying history without it leaves the following ToolResultTurn answering
    a question nobody asked.

    `calls` is a tuple because a model may request several in one turn, and
    they have to be replayed together: splitting them into separate turns
    reorders the transcript into something the model never produced.
    """
    calls: tuple[ToolCall, ...]

    @property
    def role(self) -> Literal["assistant"]:
        return "assistant"


@dataclass(frozen=True, slots=True)
class ToolResultTurn:
    """What a tool returned, going back to the model.

    `content` is the structured result — a JSON-able dict, never prose. The
    provider decides how its SDK carries that; nothing above the provider
    formats it into a sentence, because a tool result the model has to parse
    out of English is a tool result it can misread.

    `is_error` marks a ToolError. The model still sees it and still gets to
    react (ask for a missing parameter, try a different range); it is a
    result, not an exception. The flag exists so the provider can label it and
    so the turn loop can count failures without inspecting content.
    """
    name: str
    content: dict[str, Any]
    is_error: bool = False

    @property
    def role(self) -> Literal["user"]:
        # Gemini carries function responses on the user side of the
        # conversation. That is a wire detail, but it is the same wire detail
        # in every provider that models tools as messages, and exposing it as
        # a role keeps assembly's role-merging logic from needing a special
        # case for a turn with no role at all.
        return "user"


# Anything that can appear in AssembledPrompt.turns.
#
# Sibling types rather than one Turn with optional fields, and the deciding
# reason is llm/assembly.py::build_turns: it merges consecutive same-role
# turns by concatenating their text. With a widened role Literal and an
# optional tool payload, a tool result could be silently glued onto a text
# turn — invisible in the logs, baffling in the output. As separate types the
# merge cannot even be attempted: there is no .text to concatenate.
AnyTurn = Turn | ToolCallTurn | ToolResultTurn


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
    turns: tuple[AnyTurn, ...] = ()
    tools: tuple[Any, ...] = ()

    def for_log(self) -> str:
        """The exact assembled call, serialized for llm_exchanges.full_prompt.

        Rendered from the same object the provider was handed, never
        re-derived from the request: a log that can disagree with what was
        sent is worse than no log, because it will be trusted.

        Tool turns are rendered too. Without them the log shows a user asking
        a question and Friday answering with facts that appear from nowhere,
        which is exactly the kind of log that gets trusted and misread.
        """
        parts = []
        if self.system:
            parts.append(f"[system]\n{self.system}")
        if self.tools:
            parts.append(
                "[tools offered]\n"
                + ", ".join(getattr(t, "name", str(t)) for t in self.tools)
            )
        for t in self.turns:
            if isinstance(t, ToolCallTurn):
                body = "\n".join(
                    f"{c.name}({json.dumps(c.arguments, default=str)})" for c in t.calls
                )
                parts.append(f"[tool_call]\n{body}")
            elif isinstance(t, ToolResultTurn):
                tag = "tool_error" if t.is_error else "tool_result"
                parts.append(f"[{tag}: {t.name}]\n{json.dumps(t.content, default=str)}")
            else:
                parts.append(f"[{t.role}]\n{t.text}")
        return "\n\n".join(parts)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A function call the model asked for.

    `arguments` is whatever the model produced, unvalidated. Checking it
    against the tool's signature is tools/registry.py's job and happens before
    execution — see the missing-parameter gate. A ToolCall is a request, not a
    promise that the request is well formed.
    """
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # Opaque provider metadata attached to this call, replayed verbatim.
    #
    # Gemini 3.x returns a `thought_signature` on every function-call part and
    # REJECTS the next request with 400 INVALID_ARGUMENT if it is not replayed
    # alongside the call. So a tool call cannot be reconstructed from just its
    # name and arguments — something the model produced has to survive the
    # round trip untouched.
    #
    # bytes, not an SDK object, and nothing above the provider looks inside it:
    # this stays a value the layer that made it can recognize and every other
    # layer can carry. A provider with no such concept leaves it None.
    signature: bytes | None = None


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
