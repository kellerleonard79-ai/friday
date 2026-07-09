"""
agent/core.py
LLM calls only. No routing, no state, no Telegram references.
"""

import base64
import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import requests
from google import genai
from google.genai import types

import memory.activity as activity
import memory.state as state

logger = logging.getLogger("friday.core")

# Features in this module:
#   • Dual provider support — Gemini (with function-calling tools) or local
#     Ollama — selected by config['provider'].
#   • Persona assembly: AGENTS.md base prose + a config-driven block (preset,
#     snark level, approved JARVIS phrases, custom instructions).
#   • Transient-error retry/backoff for Gemini (500/503/504/429).
#   • Token/call stats persisted to system_state for the dashboard/menubar.
#   • _think() is the single synchronous LLM entry point; callers run it in an
#     executor from the async handlers.

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


_SNARK_DIRECTIVES = {
    "none":    "Tone: dry and businesslike. No quips, no jokes.",
    "medium":  "Tone: warm efficiency. The occasional dry remark is fine.",
    "maximum": "Tone: confident and witty. Dry humor is welcome when the moment is right — never at the expense of clarity.",
}


def _load_persona_base() -> str:
    import paths
    agents_path = str(paths.resource_path("AGENTS.md"))
    fallback = "You are Friday, a helpful AI assistant. Be concise and direct."
    try:
        with open(agents_path) as f:
            base = f.read().strip()
    except FileNotFoundError:
        logger.warning(
            f"Persona file missing: {agents_path} — using built-in fallback persona."
        )
        return fallback
    if not base:
        # A blank persona would hand Gemma an empty system_instruction —
        # treat it as a miss, same as the file not existing.
        logger.warning(
            f"Persona file is empty: {agents_path} — using built-in fallback persona."
        )
        return fallback
    logger.info(f"Persona loaded: {agents_path} ({len(base)} chars)")
    return base


def _compose_persona(base: str, config: dict) -> str:
    """AGENTS.md prose + a rendered block built from config['persona']."""
    p = config.get("persona") or {}
    preset = (p.get("preset") or "friday").lower()
    snark  = (p.get("snark_level") or "medium").lower()
    phrases = p.get("jarvis_phrases") or {}
    custom = (p.get("custom_instructions") or "").strip()

    parts = [base]

    preset_lines = {
        "professional": "## Mode\nProfessional secretary. Strictly utilitarian voice.",
        "butler":       "## Mode\nButler. Formal address, dry flourishes only when the moment fits.",
        "friday":       "## Mode\nF.R.I.D.A.Y. mode. Confident, capable, dry.",
    }
    parts.append(preset_lines.get(preset, preset_lines["friday"]))

    parts.append("## Tone Calibration\n" + _SNARK_DIRECTIVES.get(snark, _SNARK_DIRECTIVES["medium"]))

    if preset in ("butler", "friday"):
        enabled = [ph for ph, on in phrases.items() if on]
        if enabled:
            quoted = "\n".join(f'- "{ph}"' for ph in enabled)
            parts.append(
                "## Approved Phrases\n"
                "You may use any of these verbatim when the moment fits. "
                "Do not invent new flourishes outside this list.\n"
                f"{quoted}"
            )

    if custom:
        parts.append("## Custom Instructions\n" + custom)

    return "\n\n".join(parts)


class FridayAgent:
    def __init__(self, config: dict, conn=None):
        base_persona = _load_persona_base()
        self.persona  = _compose_persona(base_persona, config)
        self.provider = config.get("provider", "ollama")
        self._config  = config
        self._conn    = conn
        # Source label the tool wrappers stamp onto tool_calls rows. Set per
        # call in _think (tools only run on the use_tools=True chat path, which
        # the semaphore serializes, so a plain attribute is race-safe here).
        self._tool_triggered_by = "unknown"

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
            from agent.tools import make_tools
            self._tools = make_tools(conn, config, self)
        else:
            self.gemini_client = None
            ollama_cfg = config.get("ollama", {})
            self.model_name = ollama_cfg.get("model", "llama3.2:1b")
            self.max_tokens = ollama_cfg.get("max_tokens", 1000)
            self.ollama_url = ollama_cfg.get("base_url", "http://localhost:11434")
            self._tools = None

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
        propagates to _think's except block, which returns the "" sentinel the
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

    def _think(self, prompt: str, history: list | None = None,
               use_tools: bool = True, triggered_by: str = "unknown",
               images: list[tuple[bytes, str]] | None = None,
               response_json: bool = False) -> str:
        """Synchronous LLM call. Always run via run_in_executor inside async handlers.

        use_tools controls whether Gemini gets the tool list. Set False for
        prompts that supply their own data explicitly (briefings, urgency tagging).

        triggered_by labels the source of the call (user_message, briefing_morning,
        briefing_evening, poll, ...) — it is persisted on the llm_exchanges row and
        stamped onto any tool_calls this exchange spawns. Instrumentation only;
        it never changes what the model sees or does.

        images is a list of (bytes, mime_type) attached to the user turn.
        On the multimodal path history is not supported — the call is a single
        turn. response_json asks Gemini for application/json output (tools are
        skipped on that path; JSON mode and function calling don't mix).
        """
        self._last_error = None
        # Tools only ever run on the chat path; stamp the source so the tool
        # wrappers (agent/tools.py) can attribute their rows to this call.
        self._tool_triggered_by = triggered_by
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
                    contents = []
                    for turn in (history or []):
                        role = "user" if turn["role"] == "user" else "model"
                        contents.append({"role": role, "parts": [{"text": turn["content"]}]})
                    contents.append({"role": "user", "parts": [{"text": prompt}]})
                cfg_kwargs = dict(
                    system_instruction=self.persona,
                    max_output_tokens=self.max_tokens,
                )
                if response_json:
                    cfg_kwargs["response_mime_type"] = "application/json"
                elif use_tools and self._tools:
                    cfg_kwargs["tools"] = self._tools
                resp = self._gemini_generate_with_retry(
                    contents=contents,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                usage = getattr(resp, "usage_metadata", None)
                tin  = getattr(usage, "prompt_token_count", 0) or 0
                tout = getattr(usage, "candidates_token_count", 0) or 0
                self._record_call(tokens_in=tin, tokens_out=tout)
                text = (resp.text or "").strip()
                # Gemma sometimes emits an empty Markdown fence (```\n```) as its
                # post-tool reply instead of empty text, even when the tool
                # result tells it to stay silent (see propose_calendar_event in
                # agent/tools.py). That fence renders as six literal backticks
                # in Telegram. Treat any backtick-only response as empty so the
                # caller's empty-response branch handles it correctly.
                if text and not text.replace("`", "").strip():
                    text = ""
                if not text:
                    finish = None
                    try:
                        finish = resp.candidates[0].finish_reason
                    except Exception:
                        pass
                    logger.warning(
                        f"Gemini returned empty text. finish_reason={finish} usage={usage}"
                    )
                activity.record_llm_exchange(
                    self._conn, model=self.model_name, prompt=log_prompt, response=text,
                    tokens_in=tin, tokens_out=tout,
                    duration_ms=int((time.monotonic() - start_t) * 1000),
                    triggered_by=triggered_by,
                )
                return text

            else:  # ollama
                messages = [{"role": "system", "content": self.persona}]
                for turn in (history or []):
                    messages.append({"role": turn["role"], "content": turn["content"]})
                user_msg = {"role": "user", "content": prompt}
                if images:
                    # Ollama's chat API takes base64 images; only vision-capable
                    # models will actually use them.
                    user_msg["images"] = [
                        base64.b64encode(data).decode() for data, _ in images
                    ]
                messages.append(user_msg)
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
        """Extract ONE calendar event from a photo or PDF the user sent, then
        route it through the same gated_write permission card GroupMe
        scheduling uses. Synchronous — the Telegram handler runs it in an
        executor. Never writes to the calendar directly."""
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

        tz_name = (self._config.get("agent") or {}).get("timezone", "America/Chicago")
        now = datetime.now(ZoneInfo(tz_name))
        caption_line = f'\nUser caption: "{caption}"\n' if caption else ""
        prompt = (
            f"Current date and time: {now.strftime('%A, %Y-%m-%d %H:%M')} ({tz_name})\n"
            f"{caption_line}\n"
            "The attached image(s) show a flyer, screenshot, schedule, or "
            "document containing ONE event the user wants on their calendar.\n\n"
            "Reply with ONLY a JSON object (no prose, no markdown fences):\n"
            '{"title": "short descriptive title",\n'
            ' "start": "ISO 8601 start — YYYY-MM-DDTHH:MM, or YYYY-MM-DD if no time is shown",\n'
            ' "end": "ISO 8601 end, or null if not stated",\n'
            ' "location": "string or null",\n'
            ' "notes": "brief extra details, or null"}\n\n'
            "Rules:\n"
            '- Resolve ALL relative dates ("Friday", "next week", "tomorrow") '
            "against the current date above. Do NOT infer or guess the day or "
            "year beyond that.\n"
            "- Never invent a date or time that is not in the image or caption.\n"
            '- If there is no event, or its date cannot be resolved, reply {"title": null}.'
        )

        raw = (self._think(prompt, use_tools=False, triggered_by="media",
                           images=images, response_json=True) or "").strip()
        event = self._parse_media_event(raw)
        if event is None:
            telegram.send(
                "I couldn't pull a confident event out of that, sir — could you "
                "tell me the title, date, and time?"
            )
            return

        # Same source attribution the GroupMe path prepends, so the approval
        # card shows where the event came from.
        source_line = "From PDF" if is_pdf else "From photo"
        existing_notes = event.get("notes", "")
        event["notes"] = source_line + ("\n" + existing_notes if existing_notes else "")

        default_cal = (self._config.get("agent") or {}).get("default_calendar")
        from actions import calendar as apple_writer
        try:
            apple_writer.gated_write(
                event, self._conn, telegram, default_calendar=default_cal,
            )
        except Exception as e:
            logger.error(f"on_media: gated_write failed: {e}")
            telegram.send("Something went wrong drafting that event, sir.")

    @staticmethod
    def _parse_media_event(raw: str) -> dict | None:
        """LLM extraction JSON → gated_write-shaped event dict
        (title/date/start_time/end_time/notes). None if unusable."""
        # Strip ```json fences the model may add despite instructions
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.info(f"on_media: unparseable LLM output: {raw[:200]}")
            return None
        if not isinstance(data, dict) or not data.get("title") or not data.get("start"):
            return None

        def _clean(key: str) -> str:
            val = str(data.get(key) or "").strip()
            return "" if val.lower() in ("null", "none") else val

        start_raw = _clean("start")
        try:
            start_dt = datetime.fromisoformat(start_raw)
        except ValueError:
            logger.info(f"on_media: unparseable start {start_raw!r}")
            return None

        event = {
            "title": str(data["title"]).strip(),
            "date":  start_dt.date().isoformat(),
        }
        # Date-only start (no "T"/time component) means an all-day event —
        # gated_write treats a missing start_time as all-day.
        if "T" in start_raw or " " in start_raw.rstrip():
            event["start_time"] = start_dt.strftime("%H:%M")
            end_raw = _clean("end")
            if end_raw:
                try:
                    end_dt = datetime.fromisoformat(end_raw)
                    # gated_write only accepts a same-day range ending after start
                    if end_dt.date() == start_dt.date() and end_dt > start_dt:
                        event["end_time"] = end_dt.strftime("%H:%M")
                except ValueError:
                    pass

        notes_parts = []
        location = _clean("location")
        if location:
            notes_parts.append(f"Location: {location}")
        extra = _clean("notes")
        if extra:
            notes_parts.append(extra)
        if notes_parts:
            event["notes"] = "\n".join(notes_parts)
        return event
