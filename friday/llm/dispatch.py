"""
llm/dispatch.py
The chokepoint. Every LLM call in Friday goes through dispatch().

What it does, and nothing else: resolve the profile, assemble the prompt,
select the provider, set the deadline, call, retry what is worth retrying, log,
return. It does not know what a Telegram message is and does not import any
provider SDK.

Assembly is llm/assembly.py's, called from here exactly once per dispatch. The
same AssembledPrompt is handed to the provider and rendered into the exchange
log, so the row can never describe a call that did not happen.

Synchronous by design. Callers in async contexts run it in an executor, as the
Telegram handler already does. Do not make it async — an async dispatcher would
put a blocking SDK call back on the event loop, which is the shape that wedged
the pipeline in the July 9 outage.
"""

from __future__ import annotations

import dataclasses
import logging
import time

import memory.activity as activity
import memory.state as state
from llm import context, profiles
from llm.assembly import assemble
from llm.providers.base import Provider
from llm.providers.gemini import GeminiProvider
from llm.providers.ollama import OllamaProvider
from llm.types import AssembledPrompt, LLMRequest, LLMResponse, Profile

logger = logging.getLogger("friday.llm.dispatch")

# Provider selection is a real lookup off the config key, not an import at the
# call site. Adding a third provider is a line here and a file next door.
_PROVIDER_CLASSES: dict[str, type[Provider]] = {
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
}

# Two retryable failures, on different clocks. `network` means we never reached
# the API — a blocked network must fail fast and loudly instead of burning the
# whole budget rediscovering that it is blocked. `fatal` is ours to fix.
#
#   rate_limit — quota. Waiting a second accomplishes nothing, so back off hard.
#   transient  — a 5xx. Nothing is exhausted; the far side is having a bad
#                minute and a quick redial usually lands. Phase II retried
#                these and the rewrite has to keep doing so — treating a 503
#                as fatal is a user-visible failure for a fault that would
#                have cleared on its own.
_MAX_ATTEMPTS = 3
_BACKOFF_S: dict[str, tuple[float, ...]] = {
    "rate_limit": (2.0, 5.0),   # before attempts 2 and 3
    "transient":  (0.5, 1.5),
}
_RETRYABLE = tuple(_BACKOFF_S)

_provider: Provider | None = None
_conn = None
_config: dict = {}


def configure(config: dict, conn=None) -> None:
    """Build the provider and install the profile registry. Called once at
    startup, before any dispatch. Separate from dispatch() because config has
    to enter this layer somewhere and threading it through every call site is
    how a config dict ends up being read forty times a day.

    conn is the shared SQLite connection used for exchange logging. Optional:
    without it dispatch still works and simply records nothing."""
    global _provider, _conn, _config
    _conn = conn
    _config = config
    name = config.get("provider", "ollama")
    try:
        cls = _PROVIDER_CLASSES[name]
    except KeyError:
        known = ", ".join(sorted(_PROVIDER_CLASSES))
        raise ValueError(f"Unknown LLM provider {name!r}. Known: {known}") from None
    _provider = cls(config)
    profiles.install(config)
    logger.info(f"Dispatcher ready — provider={name}")


def dispatch(request: LLMRequest) -> LLMResponse:
    """Run one LLM call. Never raises for an API-level failure.

    The deadline is set here and threaded down: the provider clamps its own
    HTTP timeout to what is left, and no retry fires once it has passed. That
    is what makes the budget a single number instead of three constants that
    have to be kept in sync — nested per-attempt timeouts multiply, a deadline
    cannot.
    """
    if _provider is None:
        raise RuntimeError("llm.dispatch.configure() has not been called")

    # The registry's copy wins over the caller's — see llm/profiles.py.
    profile = profiles.get(request.profile.name)

    deadline = time.monotonic() + profile.timeout_s
    if request.deadline is not None:
        deadline = min(deadline, request.deadline)

    # The standing blocks — clock, and location if it has been warmed — go in
    # front of whatever the caller assembled, so a caller cannot forget the
    # date and cannot shadow it either.
    #
    # INJECTED, NOT FETCHED, AND NOT A TOOL. See llm/context.py: these are
    # memory reads on a path that holds the Telegram semaphore, and a model
    # that has to *ask* what day it is, is a model that can decline to and
    # then write a calendar event into the wrong week. Do not turn these into
    # tool calls in step 3.
    blocks = context.standing_blocks(_config) + request.context_blocks
    request = dataclasses.replace(
        request, deadline=deadline, profile=profile, context_blocks=blocks
    )

    # Once, before the retry loop: a retry re-sends the same bytes, and
    # re-assembling per attempt would let a mid-dispatch AGENTS.md edit change
    # the prompt between attempt 1 and attempt 2.
    prompt = assemble(request, profile, _config)

    attempt = 0
    while True:
        response = _provider.complete(request, profile, prompt)

        kind = response.error_kind
        if kind not in _RETRYABLE:
            _log_exchange(request, profile, prompt, response)
            return response

        attempt += 1
        if attempt >= _MAX_ATTEMPTS:
            logger.error(f"{kind} after {attempt} attempts — giving up.")
            _log_exchange(request, profile, prompt, response)
            return response

        backoff = _BACKOFF_S[kind]
        delay = backoff[min(attempt, len(backoff)) - 1]
        remaining = deadline - time.monotonic()
        if remaining <= delay:
            logger.error(
                f"{kind} with {remaining:.1f}s left on the deadline — not retrying."
            )
            _log_exchange(request, profile, prompt, response)
            return response

        logger.warning(
            f"{kind} (attempt {attempt}/{_MAX_ATTEMPTS}) — retrying in {delay}s"
        )
        time.sleep(delay)


def _log_exchange(request: LLMRequest, profile: Profile,
                  prompt: AssembledPrompt, response: LLMResponse) -> None:
    """Persist one dispatch to llm_exchanges, and bump the running counters the
    menubar and dashboard read out of system_state.

    Instrumentation must never fail a request: everything here is wrapped, and a
    failure is logged and dropped. A lost row beats a lost reply.
    """
    if _conn is None:
        return
    try:
        # Rendered from the object the provider was handed, never re-derived
        # from the request. A log that can disagree with what was sent is
        # worse than no log, because it will be trusted.
        full_prompt = prompt.for_log()
        if request.images:
            # llm_exchanges stores text only — mark attachments so the feed
            # shows why this prompt produced what it did.
            full_prompt = f"[{len(request.images)} image(s) attached]\n{full_prompt}"
        body = response.text if response.finish != "error" else f"[error] {response.error_message}"

        activity.record_llm_exchange(
            _conn,
            model=response.usage.model or profile.model,
            prompt=full_prompt,
            response=body,
            tokens_in=response.usage.input_tokens,
            tokens_out=response.usage.output_tokens,
            duration_ms=response.usage.latency_ms,
            triggered_by=request.triggered_by,
            profile=profile.name,
            finish=response.finish,
            error_kind=response.error_kind,
        )

        # system_state counters — /api/status and the menubar read these.
        # They moved here from FridayAgent._record_call, which dies with
        # complete() in the next commit.
        cur = state.get(_conn, "think_calls")
        calls = int(cur) + 1 if cur and cur.isdigit() else 1
        tin = int(state.get(_conn, "tokens_in") or 0) + max(0, response.usage.input_tokens)
        tout = int(state.get(_conn, "tokens_out") or 0) + max(0, response.usage.output_tokens)
        state.set_many(_conn, {"think_calls": calls, "tokens_in": tin, "tokens_out": tout})
    except Exception as e:
        logger.debug(f"exchange logging failed: {e}")
