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
    def __init__(self, config: dict, conn=None):
        self.persona  = _load_persona()
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
                resp = self.gemini_client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                text = (resp.text or "").strip()
                if not text:
                    finish = None
                    try:
                        finish = resp.candidates[0].finish_reason
                    except Exception:
                        pass
                    usage = getattr(resp, "usage_metadata", None)
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
                return r.json().get("message", {}).get("content", "").strip()

        except Exception as e:
            logger.error(f"LLM error: {e}")
            return ""
