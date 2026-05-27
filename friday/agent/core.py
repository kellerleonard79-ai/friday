"""
agent/core.py
LLM calls only. No routing, no state, no Telegram references.
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
    def __init__(self, config: dict):
        self.persona  = _load_persona()
        self.provider = config.get("provider", "ollama")

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

        logger.info(f"Agent ready — {self.provider} / {self.model_name}")

    def _think(self, prompt: str) -> str:
        """Synchronous LLM call. Always run via run_in_executor inside async handlers."""
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
                return (resp.text or "").strip()

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
