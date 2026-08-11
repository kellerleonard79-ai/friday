"""
llm/dispatch.py
The chokepoint. Every LLM call in Friday goes through dispatch().

What it does, and nothing else: resolve the profile, select the provider, set
the deadline, call, retry a rate limit, return. It does not build prompts, does
not know what a persona is, does not know what a Telegram message is, and does
not import any provider SDK.

Synchronous by design. Callers in async contexts run it in an executor, as the
Telegram handler already does. Do not make it async — an async dispatcher would
put a blocking SDK call back on the event loop, which is the shape that wedged
the pipeline in the July 9 outage.
"""

from __future__ import annotations

import dataclasses
import logging
import time

from llm import profiles
from llm.providers.base import Provider
from llm.providers.gemini import GeminiProvider
from llm.providers.ollama import OllamaProvider
from llm.types import LLMRequest, LLMResponse

logger = logging.getLogger("friday.llm.dispatch")

# Provider selection is a real lookup off the config key, not an import at the
# call site. Adding a third provider is a line here and a file next door.
_PROVIDER_CLASSES: dict[str, type[Provider]] = {
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
}

# Rate limits are the ONLY retryable failure. `network` means we never reached
# the API — a blocked network must fail fast and loudly instead of burning the
# whole budget rediscovering that it is blocked. `fatal` is ours to fix.
_MAX_ATTEMPTS = 3
_BACKOFF_S = (1.0, 2.0)  # before attempts 2 and 3

_provider: Provider | None = None


def configure(config: dict) -> None:
    """Build the provider and install the profile registry. Called once at
    startup, before any dispatch. Separate from dispatch() because config has
    to enter this layer somewhere and threading it through every call site is
    how a config dict ends up being read forty times a day."""
    global _provider
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
    request = dataclasses.replace(request, deadline=deadline, profile=profile)

    attempt = 0
    while True:
        response = _provider.complete(request, profile)

        if response.error_kind != "rate_limit":
            return response

        attempt += 1
        if attempt >= _MAX_ATTEMPTS:
            logger.error(f"Rate limited after {attempt} attempts — giving up.")
            return response

        delay = _BACKOFF_S[min(attempt, len(_BACKOFF_S)) - 1]
        remaining = deadline - time.monotonic()
        if remaining <= delay:
            logger.error(
                f"Rate limited with {remaining:.1f}s left on the deadline — "
                f"not retrying."
            )
            return response

        logger.warning(
            f"Rate limited (attempt {attempt}/{_MAX_ATTEMPTS}) — retrying in {delay}s"
        )
        time.sleep(delay)
