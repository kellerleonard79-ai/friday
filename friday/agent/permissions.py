"""
agent/permissions.py
Friday's permission gate — Telegram inline button flow.

Confirm → log and acknowledge
Edit    → redraft and re-present
Cancel  → discard
Timeout → discard after 5 minutes
"""

import logging
import queue
import time
from datetime import datetime

logger = logging.getLogger("friday.permissions")

REPLY_TIMEOUT = 300  # 5 minutes


class PermissionGate:
    def __init__(self, memory, telegram, agent):
        self.memory = memory
        self.telegram = telegram
        self.agent = agent

    def request(self, action_type: str, draft: str, context: str = "",
                action_data: dict = None) -> dict:
        """
        Send a Confirm/Edit/Cancel card via Telegram and block until the user responds.

        Returns:
            {"approved": True,  "final_draft": str}  on Confirm
            {"approved": False, "reason": str}        on Cancel / timeout
        """
        key = f"pending_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.memory.remember(key, {
            "type": action_type,
            "draft": draft,
            "context": context,
            "action_data": action_data or {},
            "status": "awaiting",
            "created_at": datetime.now().isoformat(),
        })

        self.telegram.send_permission_request(draft, key)
        logger.info(f"Permission requested: {action_type}")

        start = time.time()
        current_draft = draft

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
                logger.info("User confirmed")
                self.memory.remember(key, {
                    **( self.memory.recall(key) or {} ),
                    "status": "approved",
                    "final_draft": current_draft,
                })
                self.telegram.send("✅ Got it — logged.")
                return {"approved": True, "final_draft": current_draft}

            elif action == "cancel":
                logger.info("User cancelled")
                self.memory.remember(key, {
                    **( self.memory.recall(key) or {} ),
                    "status": "denied",
                })
                self.telegram.send("Got it — cancelled. 👍")
                return {"approved": False, "reason": "denied"}

            elif action == "edit":
                instruction = reply.get("text", "").strip()
                if not instruction:
                    self.telegram.send("No instruction received — tap Edit and type your change.")
                    continue

                logger.info(f"Edit instruction: {instruction}")
                new_draft = self._redraft(current_draft, instruction, context)
                if not new_draft:
                    self.telegram.send("Trouble redrafting — please try again.")
                    continue

                current_draft = new_draft
                self.memory.remember(key, {
                    **( self.memory.recall(key) or {} ),
                    "draft": new_draft,
                })
                self.telegram.send_permission_request(new_draft, key)

        logger.info(f"Permission timed out: {action_type}")
        self.memory.remember(key, {
            **( self.memory.recall(key) or {} ),
            "status": "timed_out",
        })
        self.telegram.send("⏰ No reply received — action cancelled.")
        return {"approved": False, "reason": "timeout"}

    def _redraft(self, original: str, instruction: str, context: str) -> str:
        prompt = (
            f"Rewrite this proposed action incorporating the user's edit instruction.\n\n"
            f"Original: {original}\n"
            f"Edit instruction: {instruction}\n"
            f"Context: {context}\n\n"
            f"Return ONLY the revised plain-text confirmation question. "
            f"Do not include ACTION:, DRAFT:, TITLE:, DATE:, or any structured fields."
        )
        raw = self.agent._think(prompt)
        if not raw:
            return ""
        for line in raw.splitlines():
            if line.upper().startswith("DRAFT:"):
                return line.partition(":")[2].strip()
        return raw.strip()
