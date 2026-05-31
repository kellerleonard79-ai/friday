"""
channels/telegram.py
Telegram interface for Friday.

Inbound: async PTB handlers — semaphore at the top of on_message serializes all processing.
Outbound: sync requests.post — fast fire-and-forget, safe to call from any context.
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ForceReply, Update
from telegram.ext import ContextTypes

import memory.state as state

logger = logging.getLogger("friday.telegram")

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_semaphore = asyncio.Semaphore(1)


class TelegramHandler:
    def __init__(self, config: dict, agent, conn: sqlite3.Connection):
        tg = config.get("telegram", config)  # accept full config or telegram sub-dict
        self.bot_token    = tg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id      = str(tg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", ""))
        self.agent        = agent
        self.conn         = conn
        self._short_term_turns = config.get("memory", {}).get("short_term_turns", 20)

    # ── Outbound (sync) ───────────────────────────────────────────────────────

    def send(self, text: str) -> bool:
        url = _API_BASE.format(token=self.bot_token, method="sendMessage")
        try:
            r = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
            if r.status_code == 200:
                logger.info(f"Sent: {text[:80]}")
                return True
            logger.error(f"Send failed {r.status_code}: {r.text[:120]}")
        except Exception as e:
            logger.error(f"Send error: {e}")
        return False

    def send_permission_request(self, draft: str, pending_key: str) -> bool:
        """Approval gate card — wired up in Phase 4."""
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{pending_key}"),
            InlineKeyboardButton("✏️ Edit",    callback_data=f"edit:{pending_key}"),
            InlineKeyboardButton("❌ Cancel",  callback_data=f"cancel:{pending_key}"),
        ]])
        url = _API_BASE.format(token=self.bot_token, method="sendMessage")
        try:
            r = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": f"📋 Friday\n\n{draft}",
                "reply_markup": keyboard.to_dict(),
            }, timeout=10)
            return r.status_code == 200
        except Exception as e:
            logger.error(f"send_permission_request error: {e}")
            return False

    # ── Inbound (async) ───────────────────────────────────────────────────────

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Entry point for all text messages. Semaphore serializes processing."""
        async with _semaphore:
            text = update.message.text.strip()
            if not text:
                return

            # Pause gate — dashboard sets system_state.paused = "true". Silent
            # ignore so the user can resume and replay history if they want.
            if state.get(self.conn, "paused") == "true":
                # Timed-pause auto-resume: clear and proceed if past the deadline.
                until = state.get(self.conn, "paused_until")
                if until:
                    try:
                        if datetime.fromisoformat(until) <= datetime.now():
                            state.set(self.conn, "paused", "false")
                            state.delete(self.conn, "paused_until")
                            logger.info("Timed pause expired — resuming.")
                        else:
                            logger.info(f"Message dropped (paused until {until}): {text[:80]}")
                            return
                    except ValueError:
                        state.delete(self.conn, "paused_until")
                        logger.info(f"Message dropped (paused, bad until): {text[:80]}")
                        return
                else:
                    logger.info(f"Message dropped (paused): {text[:80]}")
                    return

            state.set_many(self.conn, {
                "last_message_at":      datetime.now().isoformat(),
                "last_message_preview": text[:80],
            })
            logger.info(f"Message: {text[:80]}")

            rows = self.conn.execute(
                "SELECT role, content FROM conversation_history ORDER BY id DESC LIMIT ?",
                (self._short_term_turns * 2,)
            ).fetchall()
            history = [{"role": r[0], "content": r[1]} for r in reversed(rows)]

            loop = asyncio.get_running_loop()
            self.agent._last_action_emitted = None  # reset before the call
            response = await loop.run_in_executor(None, self.agent._think, text, history)
            action_emitted = getattr(self.agent, "_last_action_emitted", None)

            now_iso = datetime.now().isoformat()
            self.conn.execute(
                "INSERT INTO conversation_history (role, content, created_at) VALUES (?, ?, ?)",
                ("user", text, now_iso),
            )

            if response:
                await update.message.reply_text(response)
                assistant_log = response
            elif action_emitted == "calendar_proposal":
                # The card already went out via gated_write. Silence is correct.
                # Record a synthetic note so future turns know what happened.
                assistant_log = "[Sent calendar approval card — awaiting user response.]"
            else:
                logger.warning("Empty LLM response — sending fallback to user")
                await update.message.reply_text("Sorry, sir — I drew a blank. Try again?")
                assistant_log = "Sorry, sir — I drew a blank. Try again?"

            self.conn.execute(
                "INSERT INTO conversation_history (role, content, created_at) VALUES (?, ?, ?)",
                ("assistant", assistant_log, now_iso),
            )
            self.conn.commit()

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle inline button taps. Stale callbacks are silently discarded."""
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            return
        data = (query.data or "").strip()
        if ":" not in data:
            return
        verb, key = data.split(":", 1)
        if verb not in ("confirm", "cancel"):
            return  # 'edit' lands in Phase 5
        row = self.conn.execute(
            "SELECT action_type FROM pending_actions WHERE id = ?", (key,),
        ).fetchone()
        if not row:
            return
        action_type = row[0]
        loop = asyncio.get_running_loop()
        # New action types dispatch here in Phase 5:
        #   - "groupme_send" → actions.groupme_send.confirm_pending / cancel_pending
        #   - "gmail_draft"  → actions.gmail_draft.confirm_pending  / cancel_pending
        if action_type == "calendar_add":
            from actions import calendar as cal_action
            fn = cal_action.confirm_pending if verb == "confirm" else cal_action.cancel_pending
            await loop.run_in_executor(None, fn, key, self.conn, self)
