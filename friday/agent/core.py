"""
agent/core.py
Friday's core agent loop — Phase 2.
Adds: two-way iMessage, Apple Calendar in briefing, permission gate for actions.

Using Google Gemini API (google-genai SDK) as the AI backend.
"""

import os
import logging
import time
from datetime import datetime
from google import genai
from google.genai import types

from agent.filter import filter_groupme
from agent.memory import Memory

logger = logging.getLogger("friday.core")


def load_persona() -> str:
    """Load Friday's system prompt from AGENTS.md."""
    agents_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "AGENTS.md")
    try:
        with open(agents_path, "r") as f:
            content = f.read()
        lines = content.split("\n")
        prompt_lines = [l for l in lines if not l.startswith("#") or "You are" in l]
        return "\n".join(prompt_lines).strip()
    except FileNotFoundError:
        logger.warning("AGENTS.md not found — using default persona")
        return "You are Friday, a proactive AI scheduling assistant. Be concise and helpful."


class FridayAgent:
    def __init__(self, config: dict, memory: Memory, imessage_channel, calendar_channel=None):
        self.config = config
        self.memory = memory
        self.imessage = imessage_channel
        self.calendar = calendar_channel
        self.persona = load_persona()
        self.permissions = None  # Set after init to avoid circular reference

        # Configure Gemini
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY environment variable not set.")

        self.client = genai.Client(api_key=api_key)
        self.model_name = config.get("gemini", {}).get("model", "models/gemini-2.0-flash-lite")
        self.max_tokens = config.get("gemini", {}).get("max_tokens", 1000)

        logger.info(f"Gemini model loaded: {self.model_name}")

    def set_permissions(self, permissions):
        """Inject the permission gate after construction."""
        self.permissions = permissions

    # ── Gemini API call ───────────────────────────────────────────────────────

    def _think(self, user_message: str, context: str = "") -> str:
        """Send a prompt to Gemini and return its response."""
        prefix_parts = []

        if context:
            prefix_parts.append(f"## Current Context\n{context}")

        long_term = self.memory.recall_all()
        if long_term:
            # Only include non-pending-action, non-groupme entries as facts
            facts = []
            for k, v in long_term.items():
                if not k.startswith("pending_groupme_") and not k.startswith("pending_action_"):
                    facts.append(f"- {k}: {v}")
            if facts:
                prefix_parts.append(f"## Remembered Facts\n" + "\n".join(facts))

        recent = self.memory.get_recent_turns(
            self.config.get("memory", {}).get("short_term_turns", 20)
        )

        history = []
        for turn in recent:
            role = "user" if turn["role"] == "user" else "model"
            history.append({"role": role, "parts": [{"text": turn["content"]}]})

        full_message = user_message
        if prefix_parts:
            full_message = "\n\n".join(prefix_parts) + "\n\n" + user_message

        contents = history + [{"role": "user", "parts": [{"text": full_message}]}]

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=self.persona,
                    max_output_tokens=self.max_tokens
                )
            )
            reply = response.text

            self.memory.add_turn("user", user_message, source="internal")
            self.memory.add_turn("assistant", reply, source="friday")

            return reply

        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            return None

    # ── Reply processing ──────────────────────────────────────────────────────

    def process_user_replies(self):
        """
        Check for new replies from the user in the Friday iMessage thread.
        Handles standalone messages that aren't part of a permission flow.
        (Permission gate handles its own reply polling internally.)
        """
        replies = self.imessage.read_replies(minutes=6)

        for reply in replies:
            text = reply["text"].strip()
            lower = text.lower()

            # Skip permission gate keywords — handled by permissions.py
            if lower in ("yes", "no") or lower.startswith("edit"):
                continue

            # General conversation — let Gemini respond
            logger.info(f"Processing standalone user reply: '{text[:60]}'")
            response = self._think(
                f"The user sent you a message: {text}\n\nRespond helpfully and concisely.",
                context="User reply in Friday iMessage thread"
            )
            if response:
                self.imessage.send_to_self(f"Friday: {response}")
            time.sleep(3)

    # ── GroupMe processing ────────────────────────────────────────────────────

    def process_groupme_messages(self, messages: list, groupme_config: dict):
        """
        Filter GroupMe messages, save to memory first, then reason with Gemini.
        If action needed, go through permission gate before doing anything.
        """
        if not messages:
            return

        for msg in messages:
            should_process, reason = filter_groupme(msg, groupme_config)

            if not should_process:
                logger.debug(f"GroupMe filtered — {reason}: '{msg['text'][:60]}'")
                continue

            logger.info(f"GroupMe passed filter ({reason}): '{msg['text'][:80]}'")

            # Save to memory BEFORE calling Gemini
            memory_entry = (
                f"[GroupMe/{msg['group_name']}] "
                f"{msg['sender_name']} at {msg['created_at']}: {msg['text']}"
            )
            self.memory.remember(f"pending_groupme_{msg['id']}", memory_entry)

            prompt = f"""A new message arrived in the GroupMe group "{msg['group_name']}".

Sender: {msg['sender_name']}
Time: {msg['created_at']}
Message: {msg['text']}

Analyze this message. Does it mention a specific event, date, or time that should be added to the user's calendar or flagged for attention?

Respond in this exact format:

ACTION: [CREATE_EVENT / REMIND / NO_ACTION]
DRAFT: [A short friendly notification to show the user. If CREATE_EVENT, ask if they want it added to calendar.]
TITLE: [Event title if CREATE_EVENT, else blank]
DATE: [Date if mentioned e.g. "tomorrow", "May 9 2026", else blank]
TIME: [Time if mentioned e.g. "8:00 AM", else blank]
DURATION: [Duration in minutes if inferable, else 60]"""

            response = self._think(prompt)
            time.sleep(3)

            if not response or "NO_ACTION" in response:
                continue

            # Parse structured response from Gemini
            action_type, draft, action_data = self._parse_action_response(response, msg)

            if not draft:
                continue

            # Go through permission gate
            if self.permissions:
                result = self.permissions.request(
                    action_type=action_type,
                    draft=draft,
                    context=memory_entry,
                    action_data=action_data
                )
                if result.get("approved"):
                    logger.info(f"User approved action: {action_type}")
                    self.memory.remember(
                        f"pending_groupme_{msg['id']}",
                        f"[ACTIONED] {memory_entry}"
                    )
                else:
                    logger.info(f"Action not approved: {result.get('reason')}")
            else:
                self.imessage.send_to_self(
                    f"📬 Friday | GroupMe ({msg['group_name']})\n\n{draft}"
                )
                self.memory.remember(
                    f"pending_groupme_{msg['id']}",
                    f"[ACTIONED] {memory_entry}"
                )

    # ── Action response parser ────────────────────────────────────────────────

    def _parse_action_response(self, response: str, msg: dict) -> tuple:
        """
        Parse Gemini's structured action response into (action_type, draft, action_data).
        Falls back to a generic notification if parsing fails.
        """
        lines = response.strip().split("\n")
        fields = {}
        for line in lines:
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip().upper()] = val.strip()

        action = fields.get("ACTION", "REMIND").upper()
        draft = fields.get("DRAFT", "").strip()
        title = fields.get("TITLE", "").strip()
        date = fields.get("DATE", "").strip()
        time_val = fields.get("TIME", "").strip()
        duration = int(fields.get("DURATION", "60")) if fields.get("DURATION", "60").isdigit() else 60

        # Fallback draft if parsing failed
        if not draft:
            draft = f"New message in {msg['group_name']} from {msg['sender_name']}: {msg['text'][:100]}"

        if action == "CREATE_EVENT" and (title or date):
            return "create_event", draft, {
                "title": title or msg['text'][:50],
                "date": date,
                "time": time_val,
                "duration_minutes": duration
            }
        elif action == "REMIND":
            return "groupme_notification", draft, {}
        else:
            return "groupme_notification", draft, {}

    # ── Evening briefing ──────────────────────────────────────────────────────

    def send_evening_briefing(self):
        """
        Compose and send the daily evening briefing.
        Now includes Apple Calendar events and unactioned GroupMe items.
        """
        today = datetime.now().strftime("%A, %B %d")
        tomorrow = datetime.now().strftime("%A, %B %d")

        # Get calendar events
        calendar_section = ""
        if self.calendar and self.calendar.enabled:
            calendar_section = self.calendar.format_for_briefing(days_ahead=2)
        else:
            calendar_section = "Calendar not connected yet."

        # Get unactioned GroupMe items
        long_term = self.memory.recall_all()
        pending_items = []
        general_facts = []

        for k, v in long_term.items():
            if k.startswith("pending_groupme_") and "[ACTIONED]" not in str(v):
                pending_items.append(str(v))
            elif not k.startswith("pending_groupme_") and not k.startswith("pending_action_"):
                general_facts.append(f"- {k}: {v}")

        pending_section = ""
        if pending_items:
            pending_section = "Unactioned messages from today:\n" + \
                "\n".join(f"- {item}" for item in pending_items)

        memory_section = "\n".join(general_facts) if general_facts else "Nothing stored yet."

        prompt = f"""Compose the evening briefing for {today}.

Upcoming calendar events:
{calendar_section}

{pending_section}

General remembered facts:
{memory_section}

Instructions:
1. Greet the user warmly for the evening
2. List tomorrow's events clearly if any exist
3. Mention any unactioned messages that need attention
4. Note anything else from memory worth flagging
5. Keep it under 200 words, conversational tone
6. Sign off as Friday"""

        briefing = self._think(prompt, context=f"Evening briefing for {today}")

        if briefing:
            self.imessage.send_to_self(f"🌙 Friday — Evening Briefing\n\n{briefing}")
            logger.info("Evening briefing sent")
        else:
            logger.error("Failed to generate evening briefing")
