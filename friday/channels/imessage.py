"""
channels/imessage.py
iMessage channel adapter for Project Friday — Phase 2.
Two-way: sends messages to user and reads replies from the Friday contact thread.

Uses macOS AppleScript for sending and the Messages SQLite database for reading.
Requires Full Disk Access for Terminal in System Settings > Privacy & Security.
"""

import subprocess
import logging
import sqlite3
import os
from datetime import datetime, timedelta

logger = logging.getLogger("friday.imessage")


class iMessageChannel:
    def __init__(self, config: dict, memory):
        self.config = config
        self.memory = memory
        self.your_handle = config.get("your_imessage_handle", "")
        self.chat_db = os.path.expanduser("~/Library/Messages/chat.db")

    # ── Sending ───────────────────────────────────────────────────────────────

    def send(self, message: str, recipient: str = None) -> bool:
        """Send an iMessage. Defaults to your own handle (the Friday thread)."""
        target = recipient or self.your_handle

        if not target:
            logger.error("No iMessage handle set. Check 'your_imessage_handle' in friday_config.yaml")
            return False

        safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
        safe_target = target.replace('"', '\\"')

        script = f'''
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{safe_target}" of targetService
    send "{safe_message}" to targetBuddy
end tell
'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0:
                logger.info(f"iMessage sent: {message[:60]}...")
                return True
            else:
                logger.error(f"AppleScript error: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error("iMessage send timed out")
            return False
        except Exception as e:
            logger.error(f"Failed to send iMessage: {e}")
            return False

    def send_to_self(self, message: str) -> bool:
        """Send a message to yourself (the Friday thread)."""
        return self.send(message, self.your_handle)

    # ── Reading ───────────────────────────────────────────────────────────────

    def read_replies(self, minutes: int = 6) -> list:
        if not os.path.exists(self.chat_db):
            logger.warning("Messages database not found.")
            return []

        cutoff = datetime.now() - timedelta(minutes=minutes)
        mac_epoch = datetime(2001, 1, 1)
        cutoff_mac = (cutoff - mac_epoch).total_seconds() * 1e9

        try:
            with sqlite3.connect(f"file:{self.chat_db}?mode=ro", uri=True) as conn:
                rows = conn.execute("""
                    SELECT DISTINCT m.rowid, m.text, m.attributedBody
                    FROM message m
                    JOIN chat_message_join cmj ON cmj.message_id = m.rowid
                    JOIN chat c ON c.rowid = cmj.chat_id
                    WHERE c.chat_identifier = ?
                    AND m.date > ?
                    AND m.attributedBody IS NOT NULL
                    ORDER BY m.date DESC
                    LIMIT 10
                """, (self.your_handle, cutoff_mac)).fetchall()

            replies = []
            for row in rows:
                msg_id = str(row[0])
                text = self._extract_text(row[1], row[2])
                if not text:
                    continue

                if self.memory.is_processed("imessage_reply", msg_id):
                    continue

                self.memory.mark_processed("imessage_reply", msg_id)
                replies.append({"id": msg_id, "text": text, "source": "imessage_reply"})
                logger.info(f"Read reply from user: '{text}'")

            return replies

        except Exception as e:
            logger.error(f"Error reading iMessages: {e}")
            return []

    def _extract_text(self, row_text: str, attributed_body: bytes) -> str:
        """Extract message text from text column or attributedBody blob."""
        if row_text and row_text.strip():
            return row_text.strip()
        if not attributed_body:
            return ""
        try:
            # attributedBody is a binary NSKeyedArchiver plist
            # The raw string sits after a known byte marker
            decoded = attributed_body.decode("utf-8", errors="ignore")
            # Find the text between known NSString markers
            marker = "NSString\x94\x84\x01+"  # common marker before text
            idx = attributed_body.find(b"NSString")
            if idx != -1:
                # Skip past the marker bytes to the actual string
                chunk = attributed_body[idx + 12:]
                # Read until a null or control byte
                end = next((i for i, b in enumerate(chunk) if b < 0x20 and b not in (0x09, 0x0a)), len(chunk))
                text = chunk[:end].decode("utf-8", errors="ignore").strip()
                if text:
                    return text
        except Exception as e:
            logger.debug(f"attributedBody parse error: {e}")
        return ""
