"""
agent/core.py
The transport seam to the model. Nothing else.

This module is deliberately narrow. There is no prompt layer, no persona, no
tool layer, and no conversation-history layer in it — all four were torn down
on branch llm-layer-teardown and are being rebuilt. What is left is the part
that talks to a provider and the instrumentation around that call:

    complete(prompt, *, images=None, json=False, triggered_by="unknown") -> str

Callers hand it a finished string and get a string back. Whether and how to
inject history, a system instruction, or tool schemas is a rewrite decision,
and this layer must not assume one.

Synchronous by design. Every caller runs it via loop.run_in_executor from an
async handler; do not make it async.
"""

import base64
import logging
import os
import time

import httpx
import requests
from google import genai
from google.genai import types

import memory.activity as activity
import memory.state as state

logger = logging.getLogger("friday.core")

# What survives here:
#   • Dual provider support — Gemini or local Ollama — selected by
#     config['provider'].
#   • Transient-error retry/backoff for Gemini (500/503/504/429) and the
#     bounded transport retry. Both are load-bearing; see the constants.
#   • Token/call stats persisted to system_state for the dashboard/menubar,
#     and the llm_exchanges row written per call.
#   • The "" return and _last_error sentinel the handlers branch on.
#
# What was removed, and is being rebuilt from scratch: the tool layer
# (agent/tools.py) and dispatcher (agent/dispatcher.py); persona and
# system-instruction assembly (agent/profiles.py, AGENTS.md loading, the
# config-driven blocks, the wall-clock stamp, the location block); every
# prompt string. AGENTS.md and quips.yaml stay in the repo and in the bundle
# as source material — nothing reads AGENTS.md any more.

# Google API transient errors worth retrying. 500/503/504 = server-side
# blip, 429 = rate limit. Everything else (400 bad request, 401 auth, etc.)
# is our fault and retrying just wastes time.
_GEMINI_RETRY_CODES = ("500", "503", "504", "429")
_GEMINI_RETRY_BACKOFF_S = (1.0, 2.0)  # delays before attempts 2 and 3
# The client's HTTP timeout, in milliseconds (google-genai HttpOptions unit).
# Load-bearing: without it a request in flight when the Mac sleeps blocks its
# executor thread forever (July 9 outage).
_GEMINI_HTTP_TIMEOUT_MS = 60_000
# Timeout/transport failures (dead socket after a sleep, etc.) get at most ONE
# retry: each attempt can burn the full client timeout, so this cap keeps
# worst-case wall time (~60 + 1 + 60 ≈ 121s) under two minutes and under the
# telegram handlers' _EXECUTOR_TIMEOUT_S ceiling.
_GEMINI_TRANSPORT_MAX_RETRIES = 1


# Only the first few PDF pages are rasterized for the vision model — flyers
# and schedules front-load their content, and each page adds latency + payload.
_PDF_MAX_PAGES = 3


def _pdf_to_png_pages(pdf_bytes: bytes) -> list[bytes]:
    """Rasterize a PDF (from bytes) into one PNG per page, capped at
    _PDF_MAX_PAGES. Raises on unreadable/corrupt input."""
    import fitz  # PyMuPDF

    pages = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for i in range(min(doc.page_count, _PDF_MAX_PAGES)):
            pages.append(doc[i].get_pixmap(dpi=150).tobytes("png"))
    return pages


class FridayAgent:
    def __init__(self, config: dict, conn=None):
        self.provider = config.get("provider", "ollama")
        self._config  = config
        self._conn    = conn

        if self.provider == "gemini":
            gemini_cfg = config.get("gemini", {})
            api_key = gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                raise EnvironmentError("GEMINI_API_KEY not set.")
            # One client for every generate_content call — text, tools, and
            # the vision/media path all inherit this timeout.
            self.gemini_client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=_GEMINI_HTTP_TIMEOUT_MS),
            )
            self.model_name = gemini_cfg.get("model", "gemma-4-31b-it")
            self.max_tokens = gemini_cfg.get("max_tokens", 1000)
        else:
            self.gemini_client = None
            ollama_cfg = config.get("ollama", {})
            self.model_name = ollama_cfg.get("model", "llama3.2:1b")
            self.max_tokens = ollama_cfg.get("max_tokens", 1000)
            self.ollama_url = ollama_cfg.get("base_url", "http://localhost:11434")

        logger.info(f"Agent ready — {self.provider} / {self.model_name}")

    # ── Stats instrumentation ─────────────────────────────────────────────

    def _record_call(self, tokens_in: int = 0, tokens_out: int = 0) -> None:
        """Bump system_state counters. Best-effort — never raise from here."""
        if self._conn is None:
            return
        try:
            cur = state.get(self._conn, "think_calls")
            calls = int(cur) + 1 if cur and cur.isdigit() else 1
            tin   = int(state.get(self._conn, "tokens_in") or 0) + max(0, int(tokens_in or 0))
            tout  = int(state.get(self._conn, "tokens_out") or 0) + max(0, int(tokens_out or 0))
            state.set_many(self._conn, {
                "think_calls": calls,
                "tokens_in":   tin,
                "tokens_out":  tout,
            })
        except Exception as e:
            logger.debug(f"stat record failed: {e}")

    def _gemini_generate_with_retry(self, *, contents, config):
        """Call Gemini generate_content with backoff on transient 503/504/429
        and on timeout/transport errors (bounded harder — see
        _GEMINI_TRANSPORT_MAX_RETRIES). On final failure the exception
        propagates to complete()'s except block, which returns the "" sentinel the
        handlers turn into the "LLM error, sir" reply."""
        attempt = 0
        transport_retries_left = _GEMINI_TRANSPORT_MAX_RETRIES
        while True:
            try:
                return self.gemini_client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )
            except httpx.TransportError as e:
                # TimeoutException, ReadError, ConnectError, … — the
                # sleep-killed-socket family. Never retried indefinitely.
                if transport_retries_left <= 0 or attempt >= len(_GEMINI_RETRY_BACKOFF_S):
                    raise
                transport_retries_left -= 1
                delay = _GEMINI_RETRY_BACKOFF_S[attempt]
                logger.warning(
                    f"Gemini transport error (attempt {attempt + 1}): "
                    f"{type(e).__name__}: {e} — retrying in {delay}s"
                )
                time.sleep(delay)
                attempt += 1
            except Exception as e:
                msg = str(e)
                is_transient = any(msg.startswith(code) for code in _GEMINI_RETRY_CODES)
                if not is_transient or attempt >= len(_GEMINI_RETRY_BACKOFF_S):
                    raise
                delay = _GEMINI_RETRY_BACKOFF_S[attempt]
                logger.warning(
                    f"Gemini transient error (attempt {attempt + 1}): {msg.splitlines()[0]} — retrying in {delay}s"
                )
                time.sleep(delay)
                attempt += 1

    def complete(self, prompt: str, *,
                 images: list[tuple[bytes, str]] | None = None,
                 json: bool = False,
                 triggered_by: str = "unknown") -> str:
        """One model call. Text in, text out. Run it via run_in_executor.

        The prompt is sent verbatim as a single user turn. No system
        instruction, no tool schemas, no conversation history — a caller that
        wants context in the call puts it in `prompt`.

        images is a list of (bytes, mime_type) attached to that same turn.
        json asks Gemini for application/json output.

        triggered_by labels the source of the call (user_message,
        briefing_morning, poll, ...) and is persisted on the llm_exchanges row.
        Instrumentation only; it never changes what the model sees or does.

        Returns "" on any failure, with the first line of the error left on
        self._last_error. Handlers branch on exactly that pair — an empty
        string with _last_error set is an outage, an empty string without one
        is a model that produced no text. Do not change the contract.
        """
        self._last_error = None
        # llm_exchanges stores text only — mark attachments so the dashboard
        # feed shows why this prompt produced what it did.
        log_prompt = f"[{len(images)} image(s) attached]\n{prompt}" if images else prompt
        start_t = time.monotonic()
        try:
            if self.provider == "gemini":
                if images:
                    # Images BEFORE text, per Google's multimodal guidance. The
                    # SDK wraps this flat parts list into a single user turn.
                    contents = [
                        types.Part.from_bytes(data=data, mime_type=mime)
                        for data, mime in images
                    ] + [prompt]
                else:
                    contents = [{"role": "user", "parts": [{"text": prompt}]}]
                cfg_kwargs = dict(max_output_tokens=self.max_tokens)
                if json:
                    cfg_kwargs["response_mime_type"] = "application/json"
                resp = self._gemini_generate_with_retry(
                    contents=contents,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                usage = getattr(resp, "usage_metadata", None)
                tin = getattr(usage, "prompt_token_count", 0) or 0
                tout = getattr(usage, "candidates_token_count", 0) or 0
                self._record_call(tokens_in=tin, tokens_out=tout)
                text = (resp.text or "").strip()
                if not text:
                    finish = None
                    try:
                        finish = resp.candidates[0].finish_reason
                    except Exception:
                        pass
                    logger.warning(
                        f"Gemini returned empty text. finish_reason={finish} "
                        f"usage={getattr(resp, 'usage_metadata', None)}"
                    )
                activity.record_llm_exchange(
                    self._conn, model=self.model_name, prompt=log_prompt, response=text,
                    tokens_in=tin, tokens_out=tout,
                    duration_ms=int((time.monotonic() - start_t) * 1000),
                    triggered_by=triggered_by,
                )
                return text

            else:  # ollama
                user_msg = {"role": "user", "content": prompt}
                if images:
                    # Ollama's chat API takes base64 images; only vision-capable
                    # models will actually use them.
                    user_msg["images"] = [
                        base64.b64encode(data).decode() for data, _ in images
                    ]
                messages = [user_msg]
                r = requests.post(
                    f"{self.ollama_url}/api/chat",
                    json={
                        "model": self.model_name,
                        "messages": messages,
                        "stream": False,
                    },
                    timeout=120,
                )
                r.raise_for_status()
                data = r.json()
                tin  = data.get("prompt_eval_count", 0) or 0
                tout = data.get("eval_count", 0) or 0
                self._record_call(tokens_in=tin, tokens_out=tout)
                text = data.get("message", {}).get("content", "").strip()
                activity.record_llm_exchange(
                    self._conn, model=self.model_name, prompt=log_prompt, response=text,
                    tokens_in=tin, tokens_out=tout,
                    duration_ms=int((time.monotonic() - start_t) * 1000),
                    triggered_by=triggered_by,
                )
                return text

        except Exception as e:
            logger.error(f"LLM error: {e}")
            self._last_error = str(e).splitlines()[0][:240]
            activity.record_llm_exchange(
                self._conn, model=self.model_name, prompt=log_prompt,
                response=f"[error] {self._last_error}", tokens_in=0, tokens_out=0,
                duration_ms=int((time.monotonic() - start_t) * 1000),
                triggered_by=triggered_by,
            )
            return ""

    # ── Media → calendar event extraction ─────────────────────────────────

    def on_media(self, file_bytes: bytes, mime_type: str,
                 caption: str | None = None) -> None:
        """Accept a photo or PDF and tell the user extraction is offline.

        TORN DOWN: the prompt that asked the model for a JSON event, and
        _parse_media_event which read it back, are gone. What survives is
        everything below the prompt line — PDF rasterization, the byte/mime
        plumbing, and the corrupt/empty-file branches — because the rewrite
        needs all of it unchanged and because a corrupt PDF should still be
        named as a corrupt PDF rather than as an offline feature.

        Reports plainly rather than dropping the file silently: a user who
        sends a flyer and gets nothing back has no way to tell a broken
        pipeline from a flyer Friday judged eventless.

        Synchronous — the Telegram handler runs it in an executor.
        """
        telegram = getattr(self, "telegram_handler", None)
        if telegram is None:
            logger.error("on_media: telegram handler not bound — dropping media")
            return

        is_pdf = mime_type == "application/pdf"
        if is_pdf:
            try:
                images = [(png, "image/png") for png in _pdf_to_png_pages(file_bytes)]
            except Exception as e:
                logger.error(f"on_media: PDF rasterization failed: {e}")
                telegram.send("Couldn't read that PDF, sir — the file may be corrupted.")
                return
            if not images:
                telegram.send("That PDF appears to have no pages, sir.")
                return
        else:
            images = [(bytes(file_bytes), mime_type)]

        kind = "PDF" if is_pdf else "image"
        logger.info(
            f"on_media: extraction offline — {kind}, {len(images)} page(s)/frame(s), "
            f"caption={caption!r}"
        )
        telegram.send(
            "Event extraction from images and PDFs is offline, sir — I'm being "
            "rebuilt. Tell me the title, date, and time and I'll take it from there."
        )
