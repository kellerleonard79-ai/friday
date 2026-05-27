"""
channels/telegram.py
Telegram Bot channel for Project Friday — replaces iMessage.

Sends messages and permission gate cards via the Telegram Bot API.
Receives button callbacks (Confirm/Edit/Cancel) and text messages via
python-telegram-bot polling, running in a dedicated background thread.

Bridge pattern: async Telegram handlers write results into a queue.Queue;
the synchronous permission gate reads from it via queue.Queue.get(timeout=...).
"""

import asyncio
import logging
import os
import queue
import threading
import requests

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, Update
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

logger = logging.getLogger("friday.telegram")

_API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramChannel:
    def __init__(self, config: dict, memory):
        self.bot_token = config.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id   = str(config.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", ""))
        self.memory    = memory

        self._reply_queue: queue.Queue = queue.Queue()
        self._awaiting_edit = False

        self._app    = None
        self._loop   = None
        self._thread = None
        self._on_text_cb = None  # callable(text: str) set by start_polling()

    # ── Sync send via direct HTTP ─────────────────────────────────────────────
    # Using requests.post directly avoids the async/sync boundary for outbound
    # messages. The async Application is only needed for inbound handling.

    def send(self, text: str) -> bool:
        """Send a plain-text message to the user."""
        url = _API_BASE.format(token=self.bot_token, method="sendMessage")
        try:
            r = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
            if r.status_code == 200:
                logger.info(f"Telegram sent: {text[:80]}")
                return True
            logger.error(f"Telegram send failed {r.status_code}: {r.text[:120]}")
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
        return False

    def send_permission_request(self, draft: str, pending_key: str) -> bool:
        """Send a permission gate card with Confirm / Edit / Cancel inline buttons."""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{pending_key}"),
            InlineKeyboardButton("✏️ Edit",    callback_data=f"edit:{pending_key}"),
            InlineKeyboardButton("❌ Cancel",  callback_data=f"cancel:{pending_key}"),
        ]])
        url = _API_BASE.format(token=self.bot_token, method="sendMessage")
        try:
            r = requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": f"📋 Friday — Proposed Action\n\n{draft}",
                    "reply_markup": keyboard.to_dict(),
                },
                timeout=10,
            )
            if r.status_code == 200:
                logger.info(f"Permission request sent for key: {pending_key}")
                return True
            logger.error(f"Permission request failed {r.status_code}: {r.text[:120]}")
        except Exception as e:
            logger.error(f"send_permission_request error: {e}")
        return False

    def get_reply(self, timeout: int = 300) -> dict:
        """
        Block until a button callback or edit-instruction text arrives.
        Returns {"action": "confirm"|"cancel"|"edit", "text": "..."}.
        Raises queue.Empty if timeout expires.
        """
        return self._reply_queue.get(timeout=timeout)

    # ── Async polling — dedicated thread + event loop ─────────────────────────

    def start_polling(self, on_text_message):
        """
        Start the Telegram Application in a background daemon thread.
        on_text_message(text: str) is called for any non-command text message
        that isn't part of an active Edit flow.
        """
        self._on_text_cb = on_text_message
        self._thread = threading.Thread(
            target=self._run_polling,
            daemon=True,
            name="telegram-poll",
        )
        self._thread.start()
        logger.info("Telegram polling started")

    def stop_polling(self):
        """Signal the polling loop to stop gracefully."""
        if self._app and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._app.stop(), self._loop)

    def _run_polling(self):
        """Runs in the dedicated thread. Owns its own event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._app = Application.builder().token(self.bot_token).build()
        self._app.add_handler(CallbackQueryHandler(self._on_callback))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )
        self._app.run_polling(stop_signals=None)  # blocks until stop_polling()

    async def _on_callback(self, update: Update, context):
        """Handle Confirm / Edit / Cancel button taps."""
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            return  # stale/expired callback — ignore silently

        action, _, key = query.data.partition(":")

        if action == "edit":
            self._awaiting_edit = True
            await context.bot.send_message(
                chat_id=self.chat_id,
                text="What should I change?",
                reply_markup=ForceReply(selective=True),
            )
        else:
            self._reply_queue.put({"action": action, "key": key})

    async def _on_message(self, update: Update, context):
        """Handle incoming text messages."""
        text = update.message.text.strip()

        if self._awaiting_edit:
            self._awaiting_edit = False
            self._reply_queue.put({"action": "edit", "text": text})
        elif self._on_text_cb:
            # Deliver to the agent's direct-command / conversational handler
            self._on_text_cb(text)
