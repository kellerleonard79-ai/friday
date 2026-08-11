"""
llm/providers/ollama.py
The local-Ollama adapter.

⚠️ UNTESTED. Friday runs on `provider: gemini`; this path has not been executed
against a live Ollama since the rewrite. It exists to keep the `provider:
gemini|ollama` config key working and, more importantly, to prove the Provider
interface has no Gemini-shaped assumptions baked into it — an interface with one
implementation is an interface nobody has checked. If it turns out to be broken
in a way that requires changing llm/types.py or llm/providers/base.py, that is
the interface being wrong, and finding that out here is the point.

There is no fallback chain. Config selects one provider; a dead Ollama is a
dead provider, not a reason to silently spend money on Gemini.
"""

from __future__ import annotations

import base64
import logging
import time

import requests

from llm.providers.base import Provider, remaining_seconds, render_prompt
from llm.types import LLMRequest, LLMResponse, Profile, Usage

logger = logging.getLogger("friday.llm.ollama")

# Same rule as the Gemini adapter, same reasoning: only a fault on an
# ESTABLISHED connection earns the single redial. requests.ConnectionError
# covers DNS failure and connection-refused — for a local Ollama that means
# "the server isn't running" — and must surface as network immediately.
_REDIALABLE = (
    requests.exceptions.ReadTimeout,
    requests.exceptions.ChunkedEncodingError,
)
_UNREACHABLE = (
    requests.exceptions.ConnectionError,
    requests.exceptions.ConnectTimeout,
)


# Same split as the Gemini adapter: 429 is a refusal, 5xx is the server
# failing on its own side and worth a quick retry. Ollama rarely emits either,
# but a proxy in front of it will.
_TRANSIENT_STATUS = (500, 502, 503, 504)


def _classify(exc: BaseException) -> tuple[str, str]:
    message = str(exc).splitlines()[0][:240] if str(exc) else type(exc).__name__
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            return "rate_limit", message
        if status in _TRANSIENT_STATUS:
            return "transient", message
        return "fatal", message
    # ConnectTimeout subclasses both ConnectionError and Timeout — check the
    # unreachable set first so it never reads as a mid-request fault.
    if isinstance(exc, _UNREACHABLE) or isinstance(exc, requests.exceptions.Timeout):
        return "network", message
    return "fatal", message


def _is_redialable(exc: BaseException) -> bool:
    if isinstance(exc, _UNREACHABLE):
        return False
    return isinstance(exc, _REDIALABLE)


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, config: dict):
        ollama_cfg = config.get("ollama", {})
        self._url = ollama_cfg.get("base_url", "http://localhost:11434")

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

        message: dict = {"role": "user", "content": render_prompt(request)}
        if request.images:
            # Ollama's chat API takes base64 images; only vision-capable models
            # will actually use them.
            message["images"] = [base64.b64encode(data).decode() for data, _ in request.images]

        payload = {
            "model": profile.model,
            "messages": [message],
            "stream": False,
            "options": {
                "num_predict": profile.max_output_tokens,
                "temperature": profile.temperature,
            },
        }
        if request.response_schema is not None:
            payload["format"] = "json" if request.response_schema is True else request.response_schema

        redialed = False
        while True:
            try:
                r = requests.post(
                    f"{self._url}/api/chat",
                    json=payload,
                    timeout=max(1.0, remaining),
                )
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:
                kind, msg = _classify(e)
                remaining = remaining_seconds(request)
                if not redialed and _is_redialable(e) and remaining > 0:
                    redialed = True
                    logger.warning(f"Ollama transport fault, redialing once: {msg}")
                    continue
                logger.error(f"Ollama call failed ({kind}): {msg}")
                return _error(kind, msg)

        usage = Usage(
            input_tokens=data.get("prompt_eval_count", 0) or 0,
            output_tokens=data.get("eval_count", 0) or 0,
            latency_ms=_elapsed_ms(),
            model=profile.model,
        )
        text = (data.get("message", {}).get("content", "") or "").strip()
        finish = "length" if data.get("done_reason") == "length" else "stop"
        return LLMResponse(text=text, usage=usage, finish=finish)  # type: ignore[arg-type]
