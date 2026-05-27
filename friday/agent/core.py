"""
agent/core.py
Friday's core agent loop — Phase 2.
Adds: two-way iMessage, Apple Calendar in briefing, permission gate for actions.

Using Google Gemini API (google-genai SDK) as the AI backend.
"""

import os
import logging
import time
import requests
from datetime import datetime
from google import genai
from google.genai import types

from agent.context_window import gather_groupme_context
from agent.filter import filter_groupme, has_scheduling_signal
from agent.memory import Memory
from agent.preprocess import annotate_with_resolved_dates

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
    def __init__(self, config: dict, memory: Memory, telegram_channel, calendar_channel=None):
        self.config = config
        self.memory = memory
        self.telegram = telegram_channel
        self.calendar = calendar_channel
        self.persona = load_persona()
        self.permissions = None  # Set after init to avoid circular reference

        self.provider = config.get("provider", "gemini")

        if self.provider == "gemini":
            gemini_cfg = config.get("gemini", {})
            api_key = gemini_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise EnvironmentError("GEMINI_API_KEY not set in config or environment.")
            self.client = genai.Client(api_key=api_key)
            self.model_name = gemini_cfg.get("model", "models/gemini-2.0-flash-lite")
            self.max_tokens = gemini_cfg.get("max_tokens", 1000)
            self.ollama_base_url = None
        else:
            self.client = None
            ollama_cfg = config.get("ollama", {})
            self.model_name = ollama_cfg.get("model", "llama3.2:3b")
            self.max_tokens = ollama_cfg.get("max_tokens", 1000)
            self.ollama_base_url = ollama_cfg.get("base_url", "http://localhost:11434")

        self._stats = {
            "think_calls_total": 0,
            "tokens_in_total": 0,
            "tokens_out_total": 0,
            "last_message_at": None,
            "last_message_source": None,
            "last_message_preview": None,
        }

        logger.info(f"LLM provider: {self.provider} | model: {self.model_name}")

    def set_permissions(self, permissions):
        """Inject the permission gate after construction."""
        self.permissions = permissions

    # ── Gemini API call ───────────────────────────────────────────────────────

    def _think(self, user_message: str, context: str = "") -> str:
        """Send a prompt to Gemini and return its response."""
        prefix_parts = []

        if context:
            prefix_parts.append(f"## Current Context\n{context}")

        _OPERATIONAL_PREFIXES = (
            "pending_groupme_", "pending_action_", "groupme_last_id_",
            "groupme_context_",
        )
        long_term = self.memory.recall_all()
        if long_term:
            facts = []
            for k, v in long_term.items():
                if not any(k.startswith(p) for p in _OPERATIONAL_PREFIXES):
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
            if self.provider == "gemini":
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=self.persona,
                        max_output_tokens=self.max_tokens
                    )
                )
                reply = response.text
                try:
                    usage = response.usage_metadata
                    self._stats["tokens_in_total"] += usage.prompt_token_count or 0
                    self._stats["tokens_out_total"] += usage.candidates_token_count or 0
                except Exception:
                    pass

            else:  # ollama
                messages = []
                if self.persona:
                    messages.append({"role": "system", "content": self.persona})
                for turn in contents[:-1]:  # history turns, excluding the new user message
                    role = "assistant" if turn["role"] == "model" else turn["role"]
                    messages.append({"role": role, "content": turn["parts"][0]["text"]})
                messages.append({"role": "user", "content": contents[-1]["parts"][0]["text"]})

                payload = {
                    "model": self.model_name,
                    "messages": messages,
                    "stream": False,
                    "options": {"num_predict": self.max_tokens},
                }
                resp = requests.post(
                    f"{self.ollama_base_url}/api/chat",
                    json=payload,
                    timeout=120,
                )
                resp.raise_for_status()
                resp_data = resp.json()
                reply = resp_data["message"]["content"]
                self._stats["tokens_in_total"] += resp_data.get("prompt_eval_count", 0)
                self._stats["tokens_out_total"] += resp_data.get("eval_count", 0)

            self._stats["think_calls_total"] += 1
            self.memory.add_turn("user", user_message, source="internal")
            self.memory.add_turn("assistant", reply, source="friday")
            return reply

        except Exception as e:
            logger.error(f"LLM error ({self.provider}): {e}")
            return None

    # ── Calendar query (conversational) ──────────────────────────────────────

    def answer_calendar_query(self, query: str) -> str:
        """Answer a natural language question about the user's calendar."""
        if not self.calendar or not self.calendar.enabled:
            return "Calendar isn't connected — enable Apple Calendar in friday_config.yaml."

        query = annotate_with_resolved_dates(query, now=datetime.now())
        calendar_data = self.calendar.format_for_briefing(days_ahead=14)

        prompt = f"""The user is asking about their calendar: "{query}"

Here are their upcoming events for the next 14 days:
{calendar_data}

Answer the question directly and concisely. If no relevant events exist, say so clearly."""

        return self._think(prompt, context="Calendar query")

    # ── Telegram message handler ──────────────────────────────────────────────

    def process_telegram_message(self, text: str):
        """
        Called by TelegramChannel whenever the user sends a text message.
        Routes to direct command, calendar query, or general scheduling response.
        Button callbacks (Confirm/Cancel/Edit) are handled by the permission gate
        directly via TelegramChannel.get_reply() — they never reach here.
        """
        text = text.strip()
        lower = text.lower()

        text = annotate_with_resolved_dates(text, now=datetime.now())
        lower = text.lower()

        # Direct scheduling commands
        command_verbs = ("add", "put", "create", "schedule", "set up", "book",
                         "block off", "remind me", "move", "cancel", "delete",
                         "remove", "reschedule", "change")
        scheduling_nouns = ("calendar", "event", "meeting", "appointment",
                            "reminder", "block", "slot")
        is_direct_command = (
            any(lower.startswith(v) or f" {v} " in lower for v in command_verbs)
            and any(n in lower for n in scheduling_nouns)
        )
        if is_direct_command:
            logger.info(f"Direct command from user: '{text[:60]}'")
            self._handle_direct_command(text)
            return

        # Calendar read queries
        query_keywords = ("what's on", "what do i have", "when is", "what time",
                          "do i have", "any events", "show my", "what are my",
                          "what's my schedule", "what is on my")
        if any(kw in lower for kw in query_keywords):
            logger.info(f"Calendar query from user: '{text[:60]}'")
            response = self.answer_calendar_query(text)
            if response:
                self.telegram.send(f"Friday: {response}")
            return

        # General scheduling signal
        if not has_scheduling_signal(text):
            logger.debug(f"Message skipped — no scheduling signal: '{text[:60]}'")
            return

        self._stats["last_message_at"] = datetime.now().isoformat()
        self._stats["last_message_source"] = "telegram"
        self._stats["last_message_preview"] = text[:80]

        logger.info(f"Processing scheduling message from user: '{text[:60]}'")
        response = self._think(
            f"The user sent you a message: {text}\n\nRespond helpfully and concisely.",
            context="User message via Telegram"
        )
        if response:
            self.telegram.send(f"Friday: {response}")

    def _handle_direct_command(self, text: str):
        """Process a direct scheduling command from the user via Telegram."""
        prompt = f"""The user sent you a direct scheduling command: "{text}"

Parse this command and respond in this EXACT format. Do not add any other text.

ACTION: [CREATE_EVENT / EDIT_EVENT / DELETE_EVENT / NO_ACTION]
DRAFT: [Ask the user to confirm — e.g. "Add Tennis on Thursday May 29 at 8:00 AM — confirm?". Never use past tense. Never reference memory keys.]
TITLE: [Event title only — e.g. "Tennis". REQUIRED for CREATE_EVENT.]
NEW_TITLE: [Replacement title for EDIT_EVENT only, else blank]
DATE: [Full date e.g. "May 29 2026". REQUIRED for CREATE_EVENT. Never leave blank if a date was mentioned.]
TIME: [Time e.g. "8:00 AM", else blank]
NEW_DATE: [New date for EDIT_EVENT only, else blank]
NEW_TIME: [New time for EDIT_EVENT only, else blank]
DURATION: [Duration in minutes if mentioned, else 60]
LOCATION: [Location if mentioned, else blank]"""

        response = self._think(prompt, context="Direct command via Telegram")
        if not response or "NO_ACTION" in response:
            self.telegram.send(
                "Friday: Got it — but I couldn't parse a clear event. "
                "Try: 'Add [event name] on [date] at [time]'"
            )
            return

        synthetic_msg = {"group_name": "direct", "sender_name": "you", "text": text}
        action_type, draft, action_data = self._parse_action_response(response, synthetic_msg)

        if not draft:
            return

        self._stats["last_message_at"] = datetime.now().isoformat()
        self._stats["last_message_source"] = "telegram_direct"
        self._stats["last_message_preview"] = text[:80]

        if self.permissions:
            result = self.permissions.request(
                action_type=action_type,
                draft=draft,
                context=f"Direct command: {text}",
                action_data=action_data
            )
            if result.get("approved"):
                logger.info(f"Direct command approved: {action_type}")
        else:
            self.telegram.send(draft)

    # ── GroupMe processing ────────────────────────────────────────────────────

    def process_groupme_messages(self, messages: list, groupme_config: dict, groupme_channel=None):
        """
        Filter GroupMe messages, save to memory, then reason with Gemini.
        If action needed, go through permission gate before doing anything.
        """
        if not messages:
            return

        for msg in messages:
            should_process, reason = filter_groupme(msg, groupme_config)

            if not should_process:
                logger.debug(f"GroupMe filtered — {reason}: '{msg['text'][:60]}'")
                continue

            msg = dict(msg)  # shallow copy — don't mutate the original list element
            msg["text"] = annotate_with_resolved_dates(msg["text"], now=datetime.now())
            logger.info(f"GroupMe passed filter ({reason}): '{msg['text'][:80]}'")

            self._stats["last_message_at"] = datetime.now().isoformat()
            self._stats["last_message_source"] = f"groupme/{msg['group_name']}"
            self._stats["last_message_preview"] = msg["text"][:80]

            memory_entry = (
                f"[GroupMe/{msg['group_name']}] "
                f"{msg['sender_name']} at {msg['created_at']}: {msg['text']}"
            )
            self.memory.remember(f"pending_groupme_{msg['id']}", memory_entry)

            prompt = f"""A new message arrived in the GroupMe group "{msg['group_name']}".

Sender: {msg['sender_name']}
Time: {msg['created_at']}
Message: {msg['text']}

Analyze this message. Does it mention a specific event, date, or time that should be
added, edited, or deleted on the user's calendar, or flagged for attention?

Respond in this exact format:

ACTION: [CREATE_EVENT / EDIT_EVENT / DELETE_EVENT / REMIND / NO_ACTION]
DRAFT: [A short friendly notification to show the user. For calendar actions, ask confirmation.]
TITLE: [Event title for CREATE_EVENT, or search term for EDIT_EVENT/DELETE_EVENT, else blank]
NEW_TITLE: [Replacement title for EDIT_EVENT only, else blank]
DATE: [Date if mentioned e.g. "May 9 2026", else blank]
TIME: [Time if mentioned e.g. "8:00 AM", else blank]
NEW_DATE: [New date for EDIT_EVENT only, else blank]
NEW_TIME: [New time for EDIT_EVENT only, else blank]
DURATION: [Duration in minutes if inferable, else 60]
LOCATION: [Location if mentioned, else blank]"""

            thread_ctx = ""
            if groupme_channel:
                thread_ctx = gather_groupme_context(msg["group_id"], groupme_channel)

            response = self._think(prompt, context=thread_ctx)
            time.sleep(3)

            if not response or "NO_ACTION" in response:
                continue

            action_type, draft, action_data = self._parse_action_response(response, msg)

            if not draft:
                continue

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
                self.telegram.send(
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
        Handles CREATE_EVENT, EDIT_EVENT, DELETE_EVENT, and REMIND.
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
        new_title = fields.get("NEW_TITLE", "").strip()
        date = fields.get("DATE", "").strip()
        time_val = fields.get("TIME", "").strip()
        new_date = fields.get("NEW_DATE", "").strip()
        new_time = fields.get("NEW_TIME", "").strip()
        location = fields.get("LOCATION", "").strip()
        duration_raw = fields.get("DURATION", "60")
        duration = int(duration_raw) if duration_raw.isdigit() else 60

        # Fallback draft
        if not draft:
            draft = (f"New message in {msg['group_name']} from "
                     f"{msg['sender_name']}: {msg['text'][:100]}")

        if action == "CREATE_EVENT":
            if not title and not date:
                logger.warning("CREATE_EVENT response missing both TITLE and DATE — asking user to clarify")
                return "groupme_notification", (
                    "I understood you want to add an event, but I couldn't parse the title or date. "
                    "Try: 'Add [event name] on [date] at [time]'"
                ), {}
            return "create_event", draft, {
                "title": title or msg["text"][:50],
                "date": date,
                "time": time_val,
                "duration_minutes": duration,
                "location": location
            }

        elif action == "EDIT_EVENT" and title:
            return "edit_event", draft, {
                "title_search": title,
                "new_title": new_title,
                "new_date": new_date,
                "new_time": new_time,
                "new_duration_minutes": duration if new_date or new_time else 0,
                "new_location": location
            }

        elif action == "DELETE_EVENT" and title:
            return "delete_event", draft, {
                "title_search": title
            }

        elif action == "REMIND":
            return "groupme_notification", draft, {}

        else:
            return "groupme_notification", draft, {}

    # ── Evening briefing ──────────────────────────────────────────────────────

    def send_evening_briefing(self):
        """
        Compose and send the daily evening briefing.
        Includes Apple Calendar events and unactioned GroupMe items.
        """
        today = datetime.now().strftime("%A, %B %d")

        # Get calendar events
        if self.calendar and self.calendar.enabled:
            calendar_section = self.calendar.format_for_briefing(days_ahead=2)
        else:
            calendar_section = "Calendar not connected yet."

        # Get unactioned GroupMe items and general facts
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
            self.telegram.send(f"🌙 Friday — Evening Briefing\n\n{briefing}")
            logger.info("Evening briefing sent")
        else:
            logger.error("Failed to generate evening briefing")