"""
agent/permissions.py
Friday's permission gate — manages the Yes / No / Edit approval flow.

On "Yes" — executes the approved action (create calendar event, send message, etc.)
On "No" — discards and logs
On "Edit: ..." — redrafts and asks again

Friday NEVER acts without going through this gate.
"""

import logging
import time
from datetime import datetime

logger = logging.getLogger("friday.permissions")

REPLY_TIMEOUT = 300  # 5 minutes — how long to wait for a reply


class PermissionGate:
    def __init__(self, memory, imessage_channel, agent_core, calendar_channel=None):
        self.memory = memory
        self.imessage = imessage_channel
        self.agent = agent_core
        self.calendar = calendar_channel

    def request(self, action_type: str, draft: str, context: str = "",
                action_data: dict = None) -> dict:
        """
        Request user approval for a proposed action.

        action_type: "create_event", "send_message", "groupme_notification"
        draft:       The human-readable proposal shown to the user
        context:     Background info used for redrafting on Edit
        action_data: Structured data needed to execute the action on approval
                     e.g. {"title": "APUSH", "date": "tomorrow", "time": "8:00 AM"}

        Returns:
            {"approved": True, "final_draft": str} on Yes
            {"approved": False, "reason": str} on No/timeout
        """
        pending_key = f"pending_action_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.memory.remember(pending_key, {
            "type": action_type,
            "draft": draft,
            "context": context,
            "action_data": action_data or {},
            "status": "awaiting_approval",
            "created_at": datetime.now().isoformat()
        })

        prompt_msg = (
            f"📋 Friday — Proposed Action\n\n"
            f"{draft}\n\n"
            f"Reply: Yes / No / Edit: [your instructions]"
        )
        self.imessage.send_to_self(prompt_msg)
        logger.info(f"Permission requested for: {action_type}")

        start = time.time()
        current_draft = draft
        current_action_data = action_data or {}

        while time.time() - start < REPLY_TIMEOUT:
            time.sleep(8)
            replies = self.imessage.read_replies(minutes=2)

            for reply in replies:
                text = reply["text"].strip()
                result = self._parse_reply(
                    text, action_type, current_draft, context,
                    pending_key, current_action_data
                )
                if result is None:
                    continue

                # Handle redraft — update draft and keep waiting
                if "_redraft" in result:
                    current_draft = result["_redraft"]
                    current_action_data = result.get("_action_data", current_action_data)
                    continue

                return result

        logger.info(f"Permission timed out for: {action_type}")
        saved = self.memory.recall(pending_key) or {}
        self.memory.remember(pending_key, {**saved, "status": "timed_out"})
        self.imessage.send_to_self("⏰ Friday — no reply received, action cancelled.")
        return {"approved": False, "reason": "timeout"}

    def _parse_reply(self, text: str, action_type: str, current_draft: str,
                     context: str, pending_key: str, action_data: dict):
        """Parse a user reply. Returns result dict, redraft signal, or None."""
        lower = text.lower().strip()

        # ── Yes ──────────────────────────────────────────────────────────────
        if lower == "yes":
            logger.info("User approved action — executing")
            saved = self.memory.recall(pending_key) or {}
            self.memory.remember(pending_key, {
                **saved, "status": "approved", "final_draft": current_draft
            })
            self._execute(action_type, current_draft, action_data)
            return {"approved": True, "final_draft": current_draft}

        # ── No ───────────────────────────────────────────────────────────────
        if lower == "no":
            logger.info("User denied action")
            saved = self.memory.recall(pending_key) or {}
            self.memory.remember(pending_key, {**saved, "status": "denied"})
            self.imessage.send_to_self("Got it — action cancelled. 👍")
            return {"approved": False, "reason": "denied"}

        # ── Edit ─────────────────────────────────────────────────────────────
        if lower.startswith("edit"):
            instruction = text[4:].lstrip(":").strip()
            if not instruction:
                self.imessage.send_to_self(
                    "What should I change? Reply: Edit: [your instructions]"
                )
                return None

            logger.info(f"User requested edit: {instruction}")
            new_draft = self._redraft(current_draft, instruction, context)

            if not new_draft:
                self.imessage.send_to_self("Trouble redrafting — try again?")
                return None

            self.imessage.send_to_self(
                f"📋 Friday — Revised Draft\n\n{new_draft}\n\n"
                f"Reply: Yes / No / Edit: [your instructions]"
            )
            saved = self.memory.recall(pending_key) or {}
            self.memory.remember(pending_key, {**saved, "draft": new_draft})
            return {"_redraft": new_draft, "_action_data": action_data}

        return None

    # ── Action executor ───────────────────────────────────────────────────────

    def _execute(self, action_type: str, draft: str, action_data: dict):
        """Execute the approved action."""

        if action_type == "create_event":
            self._execute_create_event(action_data, draft)

        elif action_type == "send_message":
            recipient = action_data.get("recipient", "")
            message = action_data.get("message", draft)
            if recipient:
                success = self.imessage.send(message, recipient)
                if success:
                    self.imessage.send_to_self(f"✅ Message sent to {recipient}.")
                else:
                    self.imessage.send_to_self("❌ Failed to send message.")
            else:
                self.imessage.send_to_self("⚠️ No recipient specified — couldn't send.")

        elif action_type == "groupme_notification":
            # GroupMe notifications are informational — no further action needed
            self.imessage.send_to_self("✅ Noted and saved.")

        else:
            logger.warning(f"Unknown action type: {action_type}")
            self.imessage.send_to_self("✅ Action approved and logged.")

    def _execute_create_event(self, action_data: dict, draft: str):
        """Create an Apple Calendar event from action_data."""
        if not self.calendar:
            self.imessage.send_to_self(
                "⚠️ Calendar not connected — event not created. "
                "Enable Apple Calendar in friday_config.yaml."
            )
            return

        title = action_data.get("title", "New Event")
        date_str = action_data.get("date", "")
        time_str = action_data.get("time", "")
        duration = action_data.get("duration_minutes", 60)
        calendar_name = action_data.get("calendar", "")

        if not date_str:
            self.imessage.send_to_self(
                "⚠️ Couldn't determine the event date — please add it to your calendar manually."
            )
            return

        success = self.calendar.create_event(
            title=title,
            date_str=date_str,
            time_str=time_str,
            duration_minutes=duration,
            calendar_name=calendar_name
        )

        if success:
            time_part = f" at {time_str}" if time_str else ""
            self.imessage.send_to_self(
                f"✅ Added to calendar: {title} on {date_str}{time_part}."
            )
        else:
            self.imessage.send_to_self(
                f"❌ Couldn't create the calendar event. "
                f"Try adding '{title}' on {date_str} manually."
            )

    def _redraft(self, original_draft: str, instruction: str, context: str) -> str:
        prompt = f"""The user wants to edit this proposed action:

Original:
{original_draft}

Edit instruction: {instruction}
Context: {context}

Rewrite incorporating the instruction. Return ONLY the revised text."""
        return self.agent._think(prompt)
