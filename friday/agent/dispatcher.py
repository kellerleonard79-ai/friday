"""
agent/dispatcher.py
Decide which tools a message might need, before the expensive call is made.

Attaching all ten tool schemas costs ~2,700 tokens, and under the SDK's
automatic function calling that envelope is re-sent on every hop of the tool
loop — so one calendar add can run five figures of input tokens. A cheap call
against a small model narrows the list first; the real call carries only what
was selected.

The contract that matters most here is the failure contract. Every failure
mode — unparseable output, a hallucinated tool name, a timeout, a dead
provider — falls back to the FULL tool list, never to []. An empty list from a
garbled response is indistinguishable from a genuine "no tools needed", and
guessing wrong means Friday silently drops an action the user asked for. Slow
and correct beats fast and quietly broken.

TOOL_MANIFEST is hand-maintained and deliberately NOT derived from the tool
docstrings or the generated schema. Deriving it would reintroduce the cost the
dispatcher exists to avoid — the docstrings are ~2,700 tokens, the manifest is
~210. agent/core.py asserts the two stay in sync at startup.
"""

import concurrent.futures
import json
import logging
import time

import requests

from agent import profiles

logger = logging.getLogger("friday.dispatcher")

# One line per tool, under 12 words each. This is the whole point: it is the
# cheap description, not the schema. Keys must exactly match the registered
# tool function names in agent/tools.py — enforced at startup by
# assert_manifest_matches_tools().
TOOL_MANIFEST: dict[str, str] = {
    "get_schedule":          "Look up existing calendar events in a date range.",
    "get_weather":           "Weather, forecast, rain, or temperature.",
    "get_pending_canvas":    "Unalerted Canvas assignments and due dates.",
    "reschedule_briefing":   "Move the next morning or evening briefing once.",
    "add_calendar_event":    "Create a new calendar event.",
    "update_calendar_event": "Change an event already on the calendar.",
    "add_quip":              "Teach Friday a new phrase to say.",
    "list_quips":            "List the phrases Friday can say.",
    "remove_quip":           "Stop Friday using a phrase.",
    "update_setting":        "Change Friday's tone, preset, default calendar, or briefing times.",
}

# Outcome labels, persisted to dispatch_log.outcome.
OK = "ok"
PARSE_FAIL = "parse_fail"
TIMEOUT = "timeout"
DROPPED_INVALID = "dropped_invalid"
DISABLED = "disabled"
# Not in the original spec's enum, added after testing surfaced the need: the
# dispatcher model has its own free-tier quota, and a 429 or a 503 from it is
# not a parse failure. Folding provider errors into PARSE_FAIL would make the
# "how often is the dispatcher wrong" query read as prompt-quality noise when
# the real answer is "the upstream was down".
PROVIDER_ERROR = "provider_error"

# The Gemini API refuses a client deadline under 10s outright ("Manually set
# deadline 5s is too short"), which would turn a 5s config into a 400 on every
# call. So the transport deadline is floored here and the real budget is
# enforced client-side in _with_timeout — that also makes timeout_s mean the
# same thing on both providers instead of inheriting each one's minimum.
_MIN_TRANSPORT_TIMEOUT_S = 10.0

# One worker: dispatch is called from inside the Telegram semaphore, so calls
# are already serialized. A thread that outlives its timeout is abandoned, not
# killed — it finishes into the void and the pool reuses the slot.
_timer_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="dispatch")


def _with_timeout(fn, timeout_s: float):
    """Run fn() with a hard wall-clock ceiling, whatever the provider does.

    Raises concurrent.futures.TimeoutError if the budget is exceeded. The
    underlying HTTP call keeps its own (longer) deadline so the abandoned
    thread cannot hang forever.
    """
    return _timer_pool.submit(fn).result(timeout=timeout_s)


class ManifestMismatch(RuntimeError):
    """The manifest and the registered tools disagree. Fatal at startup.

    A tool missing from the manifest is invisible to the dispatcher, so the
    model can never select it and the tool silently stops working — the worst
    kind of bug, because everything still looks healthy. A manifest key with no
    tool behind it is the same failure seen from the other side: it goes into
    the response schema's enum, so the model can select a name that cannot be
    called.
    """


def assert_manifest_matches_tools(tools) -> None:
    """Both directions, exact. Raises ManifestMismatch on any drift.

    `tools` is the list agent/tools.py::make_tools returns. The wrappers use
    functools.wraps, so __name__ is the real tool name.
    """
    registered = {getattr(fn, "__name__", "") for fn in tools}
    listed = set(TOOL_MANIFEST)
    missing = registered - listed      # tool exists, dispatcher cannot see it
    unknown = listed - registered      # manifest promises a tool that is gone
    if missing or unknown:
        raise ManifestMismatch(
            f"TOOL_MANIFEST is out of sync with the registered tools. "
            f"Missing from manifest: {sorted(missing) or 'none'}. "
            f"In manifest but not registered: {sorted(unknown) or 'none'}. "
            f"Edit TOOL_MANIFEST in agent/dispatcher.py."
        )


class DispatchResult:
    """One dispatcher decision, with the instrumentation Step 4 logs.

    tokens_in/tokens_out stay None rather than 0 on failure — a failed call
    genuinely has no usage metadata, and writing 0 would make it average in as
    a free call.
    """

    __slots__ = ("tools", "outcome", "latency_ms", "provider", "model",
                 "tokens_in", "tokens_out", "raw")

    def __init__(self, tools, outcome, latency_ms=0, provider="", model="",
                 tokens_in=None, tokens_out=None, raw=""):
        self.tools = tools
        self.outcome = outcome
        self.latency_ms = latency_ms
        self.provider = provider
        self.model = model
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.raw = raw


class ToolDispatcher:
    def __init__(self, config: dict, gemini_api_key: str = ""):
        d = config.get("dispatcher") or {}
        self.enabled = bool(d.get("enabled", True))
        self.provider = (d.get("provider") or "gemini").lower()
        self.timeout_s = float(d.get("timeout_s", 5))
        self.model = d.get("model") or "models/gemini-2.5-flash-lite"
        self.ollama_model = d.get("ollama_model") or ""
        self._ollama_url = (config.get("ollama") or {}).get(
            "base_url", "http://localhost:11434")
        self._gemini_key = gemini_api_key
        self._client = None  # built lazily; see _gemini()

        if not self.enabled:
            logger.info("Tool dispatcher disabled — all tools attached on every call.")
        else:
            model = self.ollama_model if self.provider == "ollama" else self.model
            logger.info(
                f"Tool dispatcher ready — {self.provider} / {model} "
                f"(timeout {self.timeout_s}s, {len(TOOL_MANIFEST)} tools)"
            )

    # ── Prompt ────────────────────────────────────────────────────────────

    @staticmethod
    def _prompt(message: str) -> str:
        manifest = "\n".join(f"{n}: {d}" for n, d in TOOL_MANIFEST.items())
        return (
            f"Available tools:\n{manifest}\n\n"
            f'User message: "{message}"\n\n'
            f"Which tools might be needed? Include any that could plausibly "
            f"apply.\nReturn an empty list if none are needed."
        )

    # ── Providers ─────────────────────────────────────────────────────────

    def _gemini(self):
        """Lazy client. Separate from the agent's: different timeout, and the
        dispatcher must not inherit the 60s ceiling the chat path needs."""
        if self._client is None:
            from google import genai
            from google.genai import types
            self._client = genai.Client(
                api_key=self._gemini_key,
                http_options=types.HttpOptions(
                    timeout=int(max(self.timeout_s, _MIN_TRANSPORT_TIMEOUT_S) * 1000)),
            )
        return self._client

    def _call_gemini(self, message: str):
        """→ (raw_text, tokens_in, tokens_out). The response schema constrains
        output to an array of the exact tool names, so the model is
        structurally unable to name a tool that does not exist."""
        from google.genai import types
        resp = self._gemini().models.generate_content(
            model=self.model,
            contents=self._prompt(message),
            config=types.GenerateContentConfig(
                system_instruction=profiles.ROUTE_INSTRUCTION,
                response_mime_type="application/json",
                response_schema=types.Schema(
                    type=types.Type.ARRAY,
                    items=types.Schema(
                        type=types.Type.STRING,
                        enum=list(TOOL_MANIFEST),
                    ),
                ),
            ),
        )
        usage = getattr(resp, "usage_metadata", None)
        return (
            (resp.text or "").strip(),
            getattr(usage, "prompt_token_count", None),
            getattr(usage, "candidates_token_count", None),
        )

    def _call_ollama(self, message: str):
        """→ (raw_text, tokens_in, tokens_out).

        Ollama's structured output support varies by version: a JSON schema in
        `format` needs >= 0.5.0, older builds accept only the string "json".
        We send the schema and let a failure fall open to the full tool list
        rather than version-sniffing — and validate the result defensively
        either way, because even a schema-constrained local model is a weaker
        guarantee than Gemini's.
        """
        r = requests.post(
            f"{self._ollama_url}/api/chat",
            json={
                "model": self.ollama_model,
                "messages": [
                    {"role": "system", "content": profiles.ROUTE_INSTRUCTION},
                    {"role": "user", "content": self._prompt(message)},
                ],
                "stream": False,
                "format": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(TOOL_MANIFEST)},
                },
            },
            timeout=max(self.timeout_s, _MIN_TRANSPORT_TIMEOUT_S),
        )
        r.raise_for_status()
        data = r.json()
        return (
            (data.get("message", {}).get("content") or "").strip(),
            data.get("prompt_eval_count"),
            data.get("eval_count"),
        )

    # ── Validation ────────────────────────────────────────────────────────

    @staticmethod
    def _parse(raw: str) -> tuple[list[str] | None, str]:
        """Raw model output → (tool_names, outcome). None means fail open.

        A model that returns a genuine empty array is honored: nothing was
        dropped, so [] is a real decision. A model that returns names of which
        NONE survive validation is not honored — that is a garbled response
        wearing an empty list's clothes, and treating it as "no tools needed"
        would silently swallow the user's request.
        """
        if not raw:
            return None, PARSE_FAIL
        text = raw.strip()
        if text.startswith("```"):  # fences, despite the schema
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning(f"Dispatcher returned unparseable output: {raw[:200]!r}")
            return None, PARSE_FAIL
        if not isinstance(data, list):
            logger.warning(f"Dispatcher returned non-list output: {raw[:200]!r}")
            return None, PARSE_FAIL

        selected, dropped = [], []
        for item in data:
            name = item if isinstance(item, str) else str(item)
            if name in TOOL_MANIFEST:
                if name not in selected:
                    selected.append(name)
            else:
                dropped.append(name)
        for name in dropped:
            logger.warning(f"Dispatcher named an unknown tool, dropped: {name!r}")

        if dropped and not selected:
            return None, DROPPED_INVALID
        return selected, OK

    # ── Entry point ───────────────────────────────────────────────────────

    def dispatch(self, message: str) -> list[str]:
        """Return the tool names relevant to this message. [] means no tools."""
        return self.dispatch_detail(message).tools

    def dispatch_detail(self, message: str) -> DispatchResult:
        """dispatch() plus the instrumentation. Synchronous — call it from an
        executor, never from the event loop."""
        model = self.ollama_model if self.provider == "ollama" else self.model
        if not self.enabled:
            return DispatchResult(self._all_tools(), DISABLED,
                                  provider=self.provider, model=model)

        start = time.monotonic()
        try:
            call = (self._call_ollama if self.provider == "ollama"
                    else self._call_gemini)
            raw, tin, tout = _with_timeout(lambda: call(message), self.timeout_s)
        except Exception as e:
            elapsed = int((time.monotonic() - start) * 1000)
            # Timeouts and dead providers are the same decision: attach
            # everything and let the real call proceed. Never block chat on
            # the dispatcher.
            outcome = TIMEOUT if _is_timeout(e) else PROVIDER_ERROR
            logger.warning(
                f"Dispatcher {outcome} after {elapsed}ms "
                f"({type(e).__name__}: {str(e).splitlines()[0][:160]}) "
                f"— attaching all tools."
            )
            return DispatchResult(self._all_tools(), outcome, elapsed,
                                  self.provider, model)

        elapsed = int((time.monotonic() - start) * 1000)
        selected, outcome = self._parse(raw)
        if selected is None:
            selected = self._all_tools()
            logger.warning(f"Dispatcher {outcome} — attaching all tools.")
        logger.info(
            f"Dispatch ({elapsed}ms, {outcome}): "
            f"{', '.join(selected) if selected else '(no tools)'}"
        )
        return DispatchResult(selected, outcome, elapsed, self.provider, model,
                              tokens_in=tin, tokens_out=tout, raw=raw)

    @staticmethod
    def _all_tools() -> list[str]:
        return list(TOOL_MANIFEST)


def _is_timeout(exc: Exception) -> bool:
    """True for the timeout family across both providers' exception types.

    Type-based only. Matching on the message text looked tempting and was
    wrong: Gemini's 400 for a too-short deadline contains the word "deadline",
    so a plain config error was being logged and counted as a timeout.
    """
    return isinstance(
        exc,
        (requests.Timeout, TimeoutError, concurrent.futures.TimeoutError),
    )
