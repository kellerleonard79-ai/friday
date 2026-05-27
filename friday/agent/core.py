"""
agent/core.py
Friday's brain — LLM call and Telegram message handler.
"""

import logging
import os

import requests
from google import genai
from google.genai import types

logger = logging.getLogger("friday.core")


def _load_persona() -> str:
    agents_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "AGENTS.md")
    try:
        with open(agents_path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "You are Friday, a helpful AI assistant. Be concise and direct."


class FridayAgent:
    def __init__(self, config: dict, telegram):
        self.config = config
        self.telegram = telegram
        self.persona = _load_persona()

        self.provider = config.get("provider", "ollama")
        self._stats = {
            "think_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "last_message_at": None,
            "last_message_preview": None,
        }

        if self.provider == "gemini":
            gemini_cfg = config.get("gemini", {})
            api_key = gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
            if not api_key:
                raise EnvironmentError("GEMINI_API_KEY not set.")
            self.gemini_client = genai.Client(api_key=api_key)
            self.model_name = gemini_cfg.get("model", "models/gemini-2.5-flash-lite")
            self.max_tokens = gemini_cfg.get("max_tokens", 1000)
        else:
            self.gemini_client = None
            ollama_cfg = config.get("ollama", {})
            self.model_name = ollama_cfg.get("model", "llama3.2:1b")
            self.max_tokens = ollama_cfg.get("max_tokens", 1000)
            self.ollama_url = ollama_cfg.get("base_url", "http://localhost:11434")

        logger.info(f"Agent ready — provider={self.provider} model={self.model_name}")

    # ── LLM call ─────────────────────────────────────────────────────────────

    def _think(self, prompt: str) -> str:
        self._stats["think_calls"] += 1
        try:
            if self.provider == "gemini":
                resp = self.gemini_client.models.generate_content(
                    model=self.model_name,
                    contents=[{"role": "user", "parts": [{"text": prompt}]}],
                    config=types.GenerateContentConfig(
                        system_instruction=self.persona,
                        max_output_tokens=self.max_tokens,
                    ),
                )
                text = resp.text or ""
                try:
                    self._stats["tokens_in"]  += resp.usage_metadata.prompt_token_count or 0
                    self._stats["tokens_out"] += resp.usage_metadata.candidates_token_count or 0
                except Exception:
                    pass
                return text.strip()

            else:  # ollama
                r = requests.post(
                    f"{self.ollama_url}/api/chat",
                    json={
                        "model": self.model_name,
                        "messages": [
                            {"role": "system", "content": self.persona},
                            {"role": "user",   "content": prompt},
                        ],
                        "stream": False,
                    },
                    timeout=120,
                )
                r.raise_for_status()
                return r.json().get("message", {}).get("content", "").strip()

        except Exception as e:
            logger.error(f"LLM error: {e}")
            return ""

    # ── Telegram handler ──────────────────────────────────────────────────────

    def on_message(self, text: str) -> None:
        from datetime import datetime
        text = text.strip()
        if not text:
            return

        self._stats["last_message_at"] = datetime.now().isoformat()
        self._stats["last_message_preview"] = text[:80]
        logger.info(f"Message: {text[:80]}")

        response = self._think(text)
        if response:
            self.telegram.send(response)
