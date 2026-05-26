"""
agent/permissions.py
Friday's permission gate — manages the Yes / No / Edit approval flow.

On "Yes" — executes the approved action (create calendar event, send message, etc.)
On "No" — discards and logs
On "Edit: ..." — redrafts and asks again

Friday NEVER acts without going through this gate.
"""

import logging
import queue
import re
import time
from datetime import datetime

logger = logging.getLogger("friday.permissions")

REPLY_TIMEOUT = 300  # 5 minutes


class PermissionGate:
    def __init__(self, memory, telegram_channel, agent_core, calendar_channel=None):
        self.memory = memory
        self.telegram = telegram_channel
        self.agent = agent_core
        self.calendar = calendar_channel

    def request(self, action_type: str, draft: str, context: str = "",
                action_data: dict = None) -> dict:
        """
        Request user approval via Telegram inline buttons.

        Sends a card with Confirm / Edit / Cancel. Blocks on queue.get() until
        the user taps a button or the 5-minute timeout expires.

        Returns:
            {"approved": True, "final_draft": str} on Confirm
            {"approved": False, "reason": str} on Cancel/timeout
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

        self.telegram.send_permission_request(draft, pending_key)
        logger.info(f"Permission requested for: {action_type}")

        start = time.time()
        current_draft = draft
        current_action_data = action_data or {}

        while True:
            remaining = REPLY_TIMEOUT - (time.time() - start)
            if remaining <= 0:
                break

            try:
                reply = self.telegram.get_reply(timeout=remaining)
            except queue.Empty:
                break

            action = reply.get("action")

            if action == "confirm":
                logger.info("User confirmed action — executing")
                saved = self.memory.recall(pending_key) or {}
                self.memory.remember(pending_key, {
                    **saved, "status": "approved", "final_draft": current_draft
                })
                self._execute(action_type, current_draft, current_action_data)
                return {"approved": True, "final_draft": current_draft}

            elif action == "cancel":
                logger.info("User cancelled action")
                saved = self.memory.recall(pending_key) or {}
                self.memory.remember(pending_key, {**saved, "status": "denied"})
                self.telegram.send("Got it — action cancelled. 👍")
                return {"approved": False, "reason": "denied"}

            elif action == "edit":
                instruction = reply.get("text", "").strip()
                if not instruction:
                    self.telegram.send("No instruction received — try tapping Edit again.")
                    continue

                logger.info(f"User requested edit: {instruction}")
                new_draft = self._redraft(current_draft, instruction, context)
                if not new_draft:
                    self.telegram.send("Trouble redrafting — try again.")
                    continue

                current_action_data = self._apply_edit_to_action_data(
                    current_action_data, instruction
                )
                current_draft = new_draft
                saved = self.memory.recall(pending_key) or {}
                self.memory.remember(pending_key, {**saved, "draft": new_draft})
                self.telegram.send_permission_request(new_draft, pending_key)
                continue

        logger.info(f"Permission timed out for: {action_type}")
        saved = self.memory.recall(pending_key) or {}
        self.memory.remember(pending_key, {**saved, "status": "timed_out"})
        self.telegram.send("⏰ Friday — no reply received, action cancelled.")
        return {"approved": False, "reason": "timeout"}

    # ── Action executor ───────────────────────────────────────────────────────

    def _execute(self, action_type: str, draft: str, action_data: dict):
        """Execute the approved action."""

        if action_type == "create_event":
            self._execute_create_event(action_data, draft)

        elif action_type == "edit_event":
            self._execute_edit_event(action_data, draft)

        elif action_type == "delete_event":
            self._execute_delete_event(action_data, draft)

        elif action_type == "send_message":
            recipient = action_data.get("recipient", "")
            message = action_data.get("message", draft)
            if recipient:
                self.telegram.send(f"✅ Message noted for {recipient}: {message}")
            else:
                self.telegram.send("⚠️ No recipient specified — couldn't send.")

        elif action_type == "groupme_notification":
            self.telegram.send("✅ Noted and saved.")

        else:
            logger.warning(f"Unknown action type: {action_type}")
            self.telegram.send("✅ Action approved and logged.")

    def _execute_create_event(self, action_data: dict, draft: str):
        """Create an Apple Calendar event from action_data."""
        if not self.calendar:
            self.telegram.send(
                "⚠️ Calendar not connected — enable Apple Calendar in friday_config.yaml."
            )
            return

        title = action_data.get("title", "New Event")
        date_str = action_data.get("date", "")
        time_str = action_data.get("time", "")
        duration = action_data.get("duration_minutes", 60)
        calendar_name = action_data.get("calendar", "")

        if not date_str:
            self.telegram.send(
                "⚠️ Couldn't determine the event date — please add it manually."
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
            self.telegram.send(
                f"✅ Added to calendar: {title} on {date_str}{time_part}."
            )
        else:
            self.telegram.send(
                f"❌ Couldn't create the event. Try adding '{title}' on {date_str} manually."
            )

    def _execute_edit_event(self, action_data: dict, draft: str):
        """Edit an existing Apple Calendar event."""
        if not self.calendar:
            self.telegram.send(
                "⚠️ Calendar not connected — enable Apple Calendar in friday_config.yaml."
            )
            return

        title_search = action_data.get("title_search", "")
        if not title_search:
            self.telegram.send("⚠️ No event title to search for — edit cancelled.")
            return

        success = self.calendar.edit_event(
            title_search=title_search,
            new_title=action_data.get("new_title", ""),
            new_date_str=action_data.get("new_date", ""),
            new_time_str=action_data.get("new_time", ""),
            new_duration_minutes=action_data.get("new_duration_minutes", 0),
            new_location=action_data.get("new_location", "")
        )

        if success:
            self.telegram.send(f"✅ Calendar event updated: '{title_search}'.")
        else:
            self.telegram.send(
                f"❌ Couldn't find '{title_search}' in your calendar. "
                f"Check the event title and try again."
            )

    def _execute_delete_event(self, action_data: dict, draft: str):
        """Delete an Apple Calendar event."""
        if not self.calendar:
            self.telegram.send(
                "⚠️ Calendar not connected — enable Apple Calendar in friday_config.yaml."
            )
            return

        title_search = action_data.get("title_search", "")
        if not title_search:
            self.telegram.send("⚠️ No event title specified — delete cancelled.")
            return

        success = self.calendar.delete_event(title_search=title_search)

        if success:
            self.telegram.send(f"✅ Deleted from calendar: '{title_search}'.")
        else:
            self.telegram.send(
                f"❌ Couldn't find '{title_search}' in your calendar. "
                f"Check the event title and try again."
            )

    def _apply_edit_to_action_data(self, action_data: dict, instruction: str) -> dict:
        """Apply structured field changes from an edit instruction to action_data."""
        updated = dict(action_data)

        # Time: "7:30 AM", "7:30am", "19:30", "7 PM"
        time_match = re.search(
            r'\b(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)|\d{2}:\d{2})\b', instruction
        )
        if time_match:
            updated["time"] = time_match.group(1).strip()
            if "new_time" in action_data:
                updated["new_time"] = updated["time"]

        # Duration: "30 minutes", "1 hour", "90 min"
        dur_match = re.search(r'\b(\d+)\s*(hour|hr|minute|min)s?\b', instruction, re.I)
        if dur_match:
            val, unit = int(dur_match.group(1)), dur_match.group(2).lower()
            minutes = val * 60 if unit.startswith("h") else val
            updated["duration_minutes"] = minutes
            if "new_duration_minutes" in action_data:
                updated["new_duration_minutes"] = minutes

        # Date: ISO format "2026-05-27" or simple "May 27"
        iso_match = re.search(r'\b(\d{4}-\d{2}-\d{2})\b', instruction)
        if iso_match:
            updated["date"] = iso_match.group(1)
            if "new_date" in action_data:
                updated["new_date"] = updated["date"]

        return updated

    def _redraft(self, original_draft: str, instruction: str, context: str) -> str:
        prompt = f"""The user wants to edit this proposed action:

Original:
{original_draft}

Edit instruction: {instruction}
Context: {context}

Rewrite incorporating the instruction. Return ONLY the revised plain-text draft — no ACTION/TITLE/DATE fields."""
        raw = self.agent._think(prompt)
        if not raw:
            return ""
        # Strip Action Block leakage — return only the DRAFT line content if present
        for line in raw.splitlines():
            if line.upper().startswith("DRAFT:"):
                return line.partition(":")[2].strip()
        return raw.strip()