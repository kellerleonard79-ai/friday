"""
agent/core.py
Friday's brain — LLM reasoning and Telegram message routing.
"""

import json
import logging
import os
import time
from datetime import datetime

import requests
from google import genai
from google.genai import types

from agent.memory import Memory

logger = logging.getLogger("friday.core")

_QUERY_KEYWORDS = (
    "what", "when", "do i have", "show", "list",
    "what's on", "what do i have", "what time", "any events",
)
_OPERATIONAL_PREFIXES = ("pending_",)


def _load_persona() -> str:
    agents_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "AGENTS.md")
    try:
        with open(agents_path) as f:
            lines = [l for l in f.read().split("\n") if not l.startswith("#") or "You are" in l]
        return "\n".join(lines).strip()
    except FileNotFoundError:
        return "You are Friday, a concise AI scheduling assistant. Be brief and helpful."


class FridayAgent:
    def __init__(self, config: dict, memory: Memory, telegram, calendar=None):
        self.config = config
        self.memory = memory
        self.telegram = telegram
        self.persona = _load_persona()
        self.permissions = None  # reserved for future use

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

    def set_permissions(self, permissions) -> None:
        self.permissions = permissions

    # ── LLM call ─────────────────────────────────────────────────────────────

    def _think(self, prompt: str, context: str = "") -> str:
        """Call the LLM and return its response text."""
        parts = []
        if context:
            parts.append(f"## Context\n{context}")

        facts = {
            k: v for k, v in self.memory.recall_all().items()
            if not any(k.startswith(p) for p in _OPERATIONAL_PREFIXES)
        }
        if facts:
            lines = [f"- {k}: {v}" for k, v in facts.items()]
            parts.append("## Remembered Facts\n" + "\n".join(lines))

        recent = self.memory.get_recent_turns(
            self.config.get("memory", {}).get("short_term_turns", 20)
        )

        full_prompt = ("\n\n".join(parts) + "\n\n" + prompt).strip() if parts else prompt
        self._stats["think_calls"] += 1

        try:
            if self.provider == "gemini":
                history = []
                for turn in recent:
                    role = "user" if turn["role"] == "user" else "model"
                    history.append({"role": role, "parts": [{"text": turn["content"]}]})
                contents = history + [{"role": "user", "parts": [{"text": full_prompt}]}]
                resp = self.gemini_client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=self.persona,
                        max_output_tokens=self.max_tokens,
                    ),
                )
                text = resp.text or ""
                try:
                    usage = resp.usage_metadata
                    self._stats["tokens_in"]  += usage.prompt_token_count or 0
                    self._stats["tokens_out"] += usage.candidates_token_count or 0
                except Exception:
                    pass
                return text.strip()

            else:  # ollama
                messages = [{"role": "system", "content": self.persona}]
                for turn in recent:
                    messages.append({"role": turn["role"], "content": turn["content"]})
                messages.append({"role": "user", "content": full_prompt})

                r = requests.post(
                    f"{self.ollama_url}/api/chat",
                    json={"model": self.model_name, "messages": messages, "stream": False},
                    timeout=120,
                )
                r.raise_for_status()
                data = r.json()
                text = data.get("message", {}).get("content", "").strip()
                return text

        except Exception as e:
            logger.error(f"LLM error: {e}")
            return ""

    # ── Telegram message router ───────────────────────────────────────────────

    def on_message(self, text: str) -> None:
        """Entry point for all inbound Telegram text messages."""
        text = text.strip()
        if not text:
            return

        lower = text.lower()
        self._stats["last_message_at"] = datetime.now().isoformat()
        self._stats["last_message_preview"] = text[:80]

        self.memory.add_turn("user", text)

        starts_with_verb = any(lower.startswith(v) for v in _COMMAND_VERBS)
        contains_verb = any(f" {v} " in lower for v in _COMMAND_VERBS)
        is_question = any(lower.startswith(kw) or kw in lower for kw in _QUERY_KEYWORDS)

        if starts_with_verb or (contains_verb and not is_question):
            logger.info(f"Command: {text[:60]}")
            self._handle_command(text)
        elif is_question:
            logger.info(f"Query: {text[:60]}")
            self._handle_query(text)
        else:
            logger.info(f"General: {text[:60]}")
            response = self._think(text)
            if response:
                self.memory.add_turn("assistant", response)
                self.telegram.send(response)

    def _handle_command(self, text: str) -> None:
        prompt = f"""The user sent a scheduling command: "{text}"

Respond in this EXACT format — no other text:

ACTION: [CREATE_EVENT | EDIT_EVENT | DELETE_EVENT | NO_ACTION]
DRAFT: [Ask the user to confirm. Example: "Add Tennis on Thursday May 29 at 8:00 AM — confirm?" — always a question, never past tense]
TITLE: [Event title only, e.g. "Tennis". Required for CREATE_EVENT.]
DATE: [Full date e.g. "May 29 2026". Required for CREATE_EVENT.]
TIME: [Time e.g. "8:00 AM", else blank]
DURATION: [Minutes, default 60]"""

        response = self._think(prompt, context="Direct command")
        if not response or "NO_ACTION" in response:
            self.telegram.send(
                "I couldn't parse that command. Try: 'Add [event] on [date] at [time]'"
            )
            return

        action_type, draft, action_data = self._parse_command_response(response, text)

        if not draft:
            self.telegram.send(
                "I understood you want to make a change but couldn't parse the details. "
                "Try: 'Add [event] on [date] at [time]'"
            )
            return

        if self.permissions:
            self.permissions.request(
                action_type=action_type,
                draft=draft,
                context=f"Command: {text}",
                action_data=action_data,
            )
        else:
            self.telegram.send(draft)

    def _handle_query(self, text: str) -> None:
        response = self._think(text, context="Calendar/scheduling query from user")
        if response:
            self.memory.add_turn("assistant", response)
            self.telegram.send(response)

    def _parse_command_response(self, response: str, original_text: str) -> tuple:
        fields = {}
        for line in response.strip().splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip().upper()] = val.strip()

        action = fields.get("ACTION", "").upper()
        draft  = fields.get("DRAFT", "").strip()
        title  = fields.get("TITLE", "").strip()
        date   = fields.get("DATE", "").strip()
        time_  = fields.get("TIME", "").strip()
        dur_raw = fields.get("DURATION", "60")
        duration = int(dur_raw) if dur_raw.isdigit() else 60

        if not draft:
            draft = response.strip()[:200]

        if action == "CREATE_EVENT":
            if not title and not date:
                logger.warning("CREATE_EVENT with no TITLE or DATE")
                return "no_action", "", {}
            return "create_event", draft, {
                "title": title or original_text[:50],
                "date": date,
                "time": time_,
                "duration_minutes": duration,
            }

        if action in ("EDIT_EVENT", "DELETE_EVENT") and title:
            return action.lower(), draft, {"title_search": title}

        return "no_action", draft, {}

    # ── Evening briefing ──────────────────────────────────────────────────────

    def send_evening_briefing(self) -> None:
        today = datetime.now().strftime("%A, %B %d")
        facts = self.memory.recall_all()
        fact_lines = [
            f"- {k}: {v}" for k, v in facts.items()
            if not any(k.startswith(p) for p in _OPERATIONAL_PREFIXES)
        ]
        context = ("Stored facts:\n" + "\n".join(fact_lines)) if fact_lines else ""

        response = self._think(
            f"Compose a brief evening briefing for {today}. "
            f"Summarise anything relevant from memory and wish the user a good evening. "
            f"Keep it under 5 sentences.",
            context=context,
        )
        if response:
            self.telegram.send(f"📅 Evening Briefing — {today}\n\n{response}")
