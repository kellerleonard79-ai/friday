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
import time
import threading

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger("friday.imessage")


class _ChatDBHandler(FileSystemEventHandler):
    """Debounced FSEvents handler that fires a callback when chat.db (or its WAL) changes."""

    def __init__(self, db_path: str, callback, debounce_seconds: float = 1.5):
        abs_db = os.path.abspath(db_path)
        self._targets = {abs_db, abs_db + "-wal"}
        self._callback = callback
        self._debounce = debounce_seconds
        self._timer = None
        self._lock = threading.Lock()

    def on_modified(self, event):
        if event.is_directory or os.path.abspath(event.src_path) not in self._targets:
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            t = threading.Timer(self._debounce, self._callback)
            t.daemon = True
            t.start()
            self._timer = t


class iMessageChannel:
    def __init__(self, config: dict, memory):
        self.config = config
        self.memory = memory
        self.your_handle = config.get("your_imessage_handle", "")
        self.chat_db = os.path.expanduser("~/Library/Messages/chat.db")

    # ── Watcher ───────────────────────────────────────────────────────────────

    def start_watcher(self, on_change, debounce_seconds: float = 1.5) -> Observer:
        """Start a watchdog FSEvents observer on the Messages directory.

        Calls on_change after debounce_seconds of quiet whenever chat.db or
        its WAL file is modified.  Returns the running Observer so the caller
        can stop() it on shutdown.
        """
        watch_dir = os.path.dirname(self.chat_db)
        handler = _ChatDBHandler(self.chat_db, on_change, debounce_seconds)
        observer = Observer()
        observer.schedule(handler, path=watch_dir, recursive=False)
        observer.daemon = True
        observer.start()
        logger.info(f"iMessage watcher active on {watch_dir}")
        return observer

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

        cutoff_mac = int((time.time() - minutes * 60 - 978307200) * 1_000_000_000)

        try:
            with sqlite3.connect(f"file:{self.chat_db}?mode=ro", uri=True) as conn:
                rows = conn.execute("""
                    SELECT DISTINCT m.rowid, m.text, m.attributedBody
                    FROM message m
                    JOIN chat_message_join cmj ON cmj.message_id = m.rowid
                    JOIN chat c ON c.rowid = cmj.chat_id
                    WHERE c.chat_identifier = ?
                    AND m.date > ?
                    AND (m.text IS NOT NULL OR m.attributedBody IS NOT NULL)
                    ORDER BY m.date DESC
                    LIMIT 10
                """, (self.your_handle, cutoff_mac)).fetchall()

            # Markers that identify Friday's own outbound messages
            _friday_markers = (
                "📋 Friday", "⏰ Friday", "🌙 Friday",
                "Reply: Yes / No / Edit",
                "✅ Added to calendar", "✅ Deleted", "✅ Message sent",
                "✅ Calendar event", "✅ Noted",
                "❌ Couldn't", "⚠️",
                "Got it — action cancelled",
                "Trouble redrafting",
                "What should I change?",
                "Friday is online",
                "Friday: ",
            )

            replies = []
            for row in rows:
                msg_id = str(row[0])
                text = self._extract_text(row[1], row[2])
                if not text:
                    continue

                text = text.strip()
                if len(text) < 2 or not any(c.isalpha() for c in text):
                    continue

                # Skip known ObjC class names that leak through binary blob parsing
                _objc_noise = {"NSObject", "NSString", "NSAttributedString",
                                "NSMutableAttributedString", "NSDictionary", "NSArray"}
                if text in _objc_noise:
                    if not self.memory.is_processed("imessage_reply", msg_id):
                        self.memory.mark_processed("imessage_reply", msg_id)
                    continue

                # Skip Friday's own outbound messages — mark processed so they don't re-appear
                if any(marker in text for marker in _friday_markers):
                    if not self.memory.is_processed("imessage_reply", msg_id):
                        self.memory.mark_processed("imessage_reply", msg_id)
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

    def debug_dump_messages(self, chat_identifier: str = None, limit: int = 5):
        """Run a debug SQL query against the Messages DB and print recent rows.

        Equivalent shell command:
        sqlite3 ~/Library/Messages/chat.db \
        "SELECT m.rowid, m.text, m.attributedBody, m.is_from_me, m.date \
         FROM message m \
         JOIN chat_message_join cmj ON cmj.message_id = m.rowid \
         JOIN chat c ON c.rowid = cmj.chat_id \
         WHERE c.chat_identifier = 'kellerleonard17@gmail.com' \
         ORDER BY m.date DESC LIMIT 5;" | cat
        """
        cid = chat_identifier or self.your_handle
        try:
            with sqlite3.connect(self.chat_db) as conn:
                cur = conn.execute(
                    """
                    SELECT m.rowid, m.text, m.attributedBody, m.is_from_me, m.date
                    FROM message m
                    JOIN chat_message_join cmj ON cmj.message_id = m.rowid
                    JOIN chat c ON c.rowid = cmj.chat_id
                    WHERE c.chat_identifier = ?
                    ORDER BY m.date DESC
                    LIMIT ?
                    """,
                    (cid, limit),
                )
                rows = cur.fetchall()
                for row in rows:
                    # print a compact representation; attributedBody may be large/binary
                    print((row[0], row[1], bool(row[2]), row[3], row[4]))
                return rows
        except Exception as e:
            logger.error(f"debug_dump_messages error: {e}")
            return []

    def _extract_text(self, row_text: str, attributed_body: bytes) -> str:
        """Extract message text from text column or attributedBody blob.

        Strategy:
        1. If `row_text` is present return it.
        2. Try to parse `attributed_body` as a plist (XML) and pull text strings from
           the `$objects` array (common for archived attributedBody data).
        3. Fallback to scanning for known NSString/NSAttributedString markers and
           extract a nearby printable UTF-8 run.
        """
        if row_text and row_text.strip():
            return row_text.strip()
        if not attributed_body:
            return ""

        # 1) Try parsing attributedBody as a plist (handles both binary and XML)
        try:
            import plistlib
            plist = plistlib.loads(attributed_body)
            objects = plist.get("$objects", [])
            # Skip known NSKeyedArchiver metadata strings
            _skip = {
                "$null", "NSString", "NSMutableString", "NSObject",
                "NSAttributedString", "NSMutableAttributedString",
                "NSDictionary", "NSMutableDictionary", "NSArray",
                "NSColor", "NSFont", "NSParagraphStyle",
                "NSMutableParagraphStyle", "NSValue",
            }
            for obj in objects:
                if isinstance(obj, str) and obj not in _skip and len(obj) > 1 and any(c.isalpha() for c in obj):
                    return obj.strip()
        except Exception:
            pass

        # 2) Raw bytes fallback — exact-match only to avoid false positives
        # (Edit: not detected here — iPhone Edit replies have m.text populated)
        try:
            decoded = attributed_body.decode("utf-8", errors="ignore")
            cleaned = " ".join(decoded.split())  # collapses null bytes and whitespace runs
            lower = cleaned.lower()
            if lower == "yes":
                return "Yes"
            if lower == "no":
                return "No"
        except Exception:
            pass

        # 3) Heuristic scan for printable UTF-8 runs near known ObjC class markers
        try:
            for marker in (b"NSAttributedString", b"NSMutableAttributedString", b"NSString"):
                idx = attributed_body.find(marker)
                if idx == -1:
                    continue
                chunk = attributed_body[idx + len(marker):]
                # Find first printable ASCII char within the next 200 bytes
                start = None
                for i in range(min(200, len(chunk))):
                    if 0x20 <= chunk[i] <= 0x7E:
                        start = i
                        break
                if start is None:
                    continue
                end = start
                while end < len(chunk) and 0x20 <= chunk[end] <= 0x7E:
                    end += 1
                try:
                    text = chunk[start:end].decode("utf-8", errors="ignore").strip()
                except Exception:
                    text = ""
                if text and any(c.isalpha() for c in text):
                    return text
        except Exception as e:
            logger.debug(f"attributedBody heuristic parse error: {e}")

        return ""
    