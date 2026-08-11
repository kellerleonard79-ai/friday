"""
llm/providers/gemini.py
The Gemini adapter. THE ONLY FILE IN THE REPOSITORY THAT IMPORTS google.genai.

Everything Gemini-shaped stops here: the SDK's contents/config shape, its error
classes, its usage metadata field names. Above this file the rest of Friday
speaks only LLMRequest/LLMResponse.

Never retries a policy failure — that is the dispatcher's job. Never raises for
an API-level failure — returns finish="error" with an error_kind instead.
"""

from __future__ import annotations

import logging
import time

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from llm.providers.base import Provider, remaining_seconds, render_prompt
from llm.types import LLMRequest, LLMResponse, Profile, Usage

logger = logging.getLogger("friday.llm.gemini")

# Hard ceiling on any single HTTP call, in milliseconds (google-genai HttpOptions
# unit). The per-call timeout is min(this, time left before the deadline), so
# this is a floor on responsiveness, not the budget itself — the deadline is.
#
# Load-bearing: without an HTTP-level timeout, a request in flight when the Mac
# sleeps blocks its executor thread forever while holding the Telegram semaphore
# (the July 9 outage). A Python-side wrapper cannot fix this — asyncio.wait_for
# does not kill an executor thread. Only the SDK client's own timeout does.
_HTTP_TIMEOUT_CEILING_MS = 60_000

# Transport faults that mean "this established connection died mid-request".
# A socket the OS killed during sleep is the canonical case: it redials
# instantly and succeeds. ONE redial, and only if the deadline still allows.
#
# ConnectError and ConnectTimeout are deliberately NOT here. DNS failure and
# connection-refused arrive as ConnectError, and those are genuine "cannot
# reach this network" signals that must surface as network immediately. A
# redial on those is the slow-fail that makes a blocked network painful —
# which matters on the school network, and matters more once this same
# classification covers Telegram and OAuth.
_REDIALABLE = (
    httpx.ReadError,
    httpx.WriteError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)

# Transport faults that are network but must NOT be redialed.
_UNREACHABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
)

_QUOTA_MARKERS = ("RESOURCE_EXHAUSTED", "quota", "rate limit", "429")

# Server-side faults. Google returns these under real load and they succeed on
# a redial seconds later — Phase II retried them and this layer has to as well.
# Matched on the SDK's parsed status code, never on a bare "500" in the message
# text: a token count or a model name can contain those digits.
_TRANSIENT_CODES = (500, 502, 503, 504)
_TRANSIENT_MARKERS = ("UNAVAILABLE", "INTERNAL", "overloaded")


def _walk(exc: BaseException):
    """The exception and everything it wraps. The SDK re-raises httpx errors
    inside its own types, so the interesting class is often not the outermost."""
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _is_redialable(exc: BaseException) -> bool:
    """True only for a fault on an already-established connection."""
    for e in _walk(exc):
        if isinstance(e, _UNREACHABLE):
            return False
        if isinstance(e, _REDIALABLE):
            return True
    return False


def _classify(exc: BaseException) -> tuple[str, str]:
    """(error_kind, first line of the message).

    429 and quota exhaustion are rate_limit — the API answered and refused us.
    5xx is transient — the API answered and failed on its own side. Anything
    transport-level is network — we never got an answer. Everything else,
    including every 4xx, is fatal: a bad key or a malformed request will fail
    identically forever and retrying it only spends the deadline.
    """
    message = str(exc).splitlines()[0][:240] if str(exc) else type(exc).__name__

    for e in _walk(exc):
        if isinstance(e, genai_errors.APIError):
            code = getattr(e, "code", None)
            if code == 429:
                return "rate_limit", message
            if code in _TRANSIENT_CODES:
                return "transient", message
            break
        if isinstance(e, (httpx.TransportError, httpx.TimeoutException)):
            return "network", message

    lowered = message.lower()
    if any(m.lower() in lowered for m in _QUOTA_MARKERS):
        return "rate_limit", message
    if any(m.lower() in lowered for m in _TRANSIENT_MARKERS):
        return "transient", message
    return "fatal", message


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, config: dict):
        import os

        gemini_cfg = config.get("gemini", {})
        api_key = gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY not set.")
        # One client for every call. Its timeout is only the fallback default —
        # each call overrides it from the deadline via GenerateContentConfig.
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=_HTTP_TIMEOUT_CEILING_MS),
        )

    # ── Request translation ───────────────────────────────────────────────

    def _contents(self, request: LLMRequest):
        text = render_prompt(request)
        if request.images:
            # Images BEFORE text, per Google's multimodal guidance. The SDK
            # wraps this flat parts list into a single user turn.
            return [
                types.Part.from_bytes(data=data, mime_type=mime)
                for data, mime in request.images
            ] + [text]
        return [{"role": "user", "parts": [{"text": text}]}]

    def _config(self, request: LLMRequest, profile: Profile, timeout_ms: int):
        kwargs = dict(
            max_output_tokens=profile.max_output_tokens,
            temperature=profile.temperature,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
        if request.response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            # A bare `True` means "JSON, shape unspecified" — the old json=
            # flag's behavior. Anything else is a real schema.
            if request.response_schema is not True:
                kwargs["response_schema"] = request.response_schema
        return types.GenerateContentConfig(**kwargs)

    # ── The call ──────────────────────────────────────────────────────────

    def complete(self, request: LLMRequest, profile: Profile) -> LLMResponse:
        started = time.monotonic()

        def _elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        def _error(kind: str, message: str) -> LLMResponse:
            return LLMResponse(
                text="",
                usage=Usage(latency_ms=_elapsed_ms(), model=profile.model),
                finish="error",
                error_kind=kind,  # type: ignore[arg-type]
                error_message=message,
            )

        remaining = remaining_seconds(request)
        if remaining <= 0:
            return _error("network", "deadline exceeded before the call was made")

        contents = self._contents(request)
        redialed = False

        while True:
            timeout_ms = int(min(_HTTP_TIMEOUT_CEILING_MS, remaining * 1000))
            try:
                resp = self._client.models.generate_content(
                    model=profile.model,
                    contents=contents,
                    config=self._config(request, profile, timeout_ms),
                )
                break
            except Exception as e:
                kind, message = _classify(e)
                remaining = remaining_seconds(request)
                if not redialed and _is_redialable(e) and remaining > 0:
                    # A dead socket redials instantly — no backoff. Exactly one.
                    redialed = True
                    logger.warning(
                        f"Gemini transport fault, redialing once: "
                        f"{type(e).__name__}: {message}"
                    )
                    continue
                if kind == "network":
                    logger.warning(f"Gemini unreachable: {message}")
                elif kind == "transient":
                    # Warning, not error: the dispatcher retries this one and
                    # it usually succeeds. Only the give-up line is an error.
                    logger.warning(f"Gemini server fault: {message}")
                else:
                    logger.error(f"Gemini call failed ({kind}): {message}")
                return _error(kind, message)

        usage_md = getattr(resp, "usage_metadata", None)
        usage = Usage(
            input_tokens=getattr(usage_md, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage_md, "candidates_token_count", 0) or 0,
            latency_ms=_elapsed_ms(),
            model=profile.model,
        )

        finish_reason = None
        try:
            finish_reason = resp.candidates[0].finish_reason
        except Exception:
            pass

        text = (getattr(resp, "text", "") or "").strip()
        if not text:
            logger.warning(
                f"Gemini returned empty text. finish_reason={finish_reason} usage={usage_md}"
            )

        finish = "length" if str(finish_reason).endswith("MAX_TOKENS") else "stop"
        return LLMResponse(text=text, usage=usage, finish=finish)  # type: ignore[arg-type]
