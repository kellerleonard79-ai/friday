"""
agent/core.py
LLM calls only. No routing, no state, no Telegram references.
"""

import logging
import os
import time

import requests
from google import genai
from google.genai import types

import memory.state as state

logger = logging.getLogger("friday.core")

# Google API transient errors worth retrying. 503/504 = server overload,
# 429 = rate limit. Everything else (400 bad request, 401 auth, etc.) is
# our fault and retrying just wastes time.
_GEMINI_RETRY_CODES = ("503", "504", "429")
_GEMINI_RETRY_BACKOFF_S = (1.0, 2.0)  # delays before attempts 2 and 3


_SNARK_DIRECTIVES = {
    "none":    "Tone: dry and businesslike. No quips, no jokes.",
    "medium":  "Tone: warm efficiency. The occasional dry remark is fine.",
    "maximum": "Tone: confident and witty. Dry humor is welcome when the moment is right — never at the expense of clarity.",
}


def _load_persona_base() -> str:
    agents_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "AGENTS.md")
    try:
        with open(agents_path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "You are Friday, a helpful AI assistant. Be concise and direct."


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
        "butler":       "## Mode\nButler. Formal address, occasional JARVIS-style flourishes drawn ONLY from the approved phrases below.",
        "friday":       "## Mode\nF.R.I.D.A.Y. mode. Confident, capable, dry. Use approved phrases below when context fits — never force them.",
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

        if self.provider == "gemini":
            gemini_cfg = config.get("gemini", {})
            api_key = gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                raise EnvironmentError("GEMINI_API_KEY not set.")
            self.gemini_client = genai.Client(api_key=api_key)
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
        """Call Gemini generate_content with backoff on transient 503/504/429.
        Other errors propagate immediately to the caller's except block."""
        attempt = 0
        while True:
            try:
                return self.gemini_client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )
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
               use_tools: bool = True) -> str:
        """Synchronous LLM call. Always run via run_in_executor inside async handlers.

        use_tools controls whether Gemini gets the tool list. Set False for
        prompts that supply their own data explicitly (briefings, urgency tagging).
        """
        try:
            if self.provider == "gemini":
                contents = []
                for turn in (history or []):
                    role = "user" if turn["role"] == "user" else "model"
                    contents.append({"role": role, "parts": [{"text": turn["content"]}]})
                contents.append({"role": "user", "parts": [{"text": prompt}]})
                cfg_kwargs = dict(
                    system_instruction=self.persona,
                    max_output_tokens=self.max_tokens,
                )
                if use_tools and self._tools:
                    cfg_kwargs["tools"] = self._tools
                resp = self._gemini_generate_with_retry(
                    contents=contents,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                usage = getattr(resp, "usage_metadata", None)
                self._record_call(
                    tokens_in=getattr(usage, "prompt_token_count", 0) or 0,
                    tokens_out=getattr(usage, "candidates_token_count", 0) or 0,
                )
                text = (resp.text or "").strip()
                if not text:
                    finish = None
                    try:
                        finish = resp.candidates[0].finish_reason
                    except Exception:
                        pass
                    logger.warning(
                        f"Gemini returned empty text. finish_reason={finish} usage={usage}"
                    )
                return text

            else:  # ollama
                messages = [{"role": "system", "content": self.persona}]
                for turn in (history or []):
                    messages.append({"role": turn["role"], "content": turn["content"]})
                messages.append({"role": "user", "content": prompt})
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
                self._record_call(
                    tokens_in=data.get("prompt_eval_count", 0) or 0,
                    tokens_out=data.get("eval_count", 0) or 0,
                )
                return data.get("message", {}).get("content", "").strip()

        except Exception as e:
            logger.error(f"LLM error: {e}")
            return ""
