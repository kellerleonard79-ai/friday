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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import memory.state as state
from agent.turn import run_turn
from effects import runner as effects_runner
from llm import profiles
from llm.types import LLMRequest

logger = logging.getLogger("friday.telegram")

_API_BASE = "https://api.telegram.org/bot{token}/{method}"

# Features handled here:
#   • on_message  — the single serialized entry point for ALL user text. The
#     semaphore below is acquired first thing so messages queue in arrival
#     order before any DB/LLM work. Also enforces the pause gate (incl. timed
#     auto-resume), rolling conversation_history, and the calendar-add
#     "confirmation already sent" special case.
#   • on_callback — inline-button taps for the approval-gate cards
#     (confirm/cancel → actions/calendar.py). 'edit' is reserved for Phase 5.
#   • send / send_permission_request — synchronous outbound helpers.
# RULE (CLAUDE.md): this semaphore must stay at the top of on_message. Never move it.
_semaphore = asyncio.Semaphore(1)

# Ceiling on any single executor call made while holding the semaphore. It is
# now a backstop, not the budget: llm/dispatch.py enforces the CHAT profile's
# 120s deadline and the provider clamps its own HTTP timeout to what is left,
# so the deadline expires first by design. This stays because wait_for cannot
# kill an executor thread — if a blocking call ever hangs past its deadline
# anyway, this is what releases the pipeline (July 9 outage).
_EXECUTOR_TIMEOUT_S = 150


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
                "text": f"Friday\n\n{draft}",
                "reply_markup": keyboard.to_dict(),
            }, timeout=10)
            return r.status_code == 200
        except Exception as e:
            logger.error(f"send_permission_request error: {e}")
            return False

    # ── Inbound (async) ───────────────────────────────────────────────────────

    def _pause_active(self, what: str) -> bool:
        """Pause gate — dashboard sets system_state.paused = "true". Silent
        drop so the user can resume and replay history if they want. Handles
        timed auto-resume: clears the pause once paused_until has passed."""
        if state.get(self.conn, "paused") != "true":
            return False
        until = state.get(self.conn, "paused_until")
        if until:
            try:
                if datetime.fromisoformat(until) <= datetime.now():
                    state.set(self.conn, "paused", "false")
                    state.delete(self.conn, "paused_until")
                    logger.info("Timed pause expired — resuming.")
                    return False
            except ValueError:
                state.delete(self.conn, "paused_until")
        logger.info(f"Dropped while paused: {what}")
        return True

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Entry point for all text messages. Semaphore serializes processing."""
        async with _semaphore:
            text = update.message.text.strip()
            if not text:
                return

            if self._pause_active(f"text: {text[:80]}"):
                return

            state.set_many(self.conn, {
                "last_message_at":      datetime.now().isoformat(),
                "last_message_preview": text[:80],
            })
            logger.info(f"Message: {text[:80]}")

            # Unchanged from before the dispatcher: same query, same window.
            # Rows come back newest-first; the request carries them oldest-first
            # because rendering them is now the provider's job, not this file's.
            rows = self.conn.execute(
                "SELECT role, content FROM conversation_history ORDER BY id DESC LIMIT ?",
                (self._short_term_turns * 2,)
            ).fetchall()
            request = LLMRequest(
                profile=profiles.get("CHAT"),
                prompt=text,
                history=tuple((role, content) for role, content in reversed(rows)),
                triggered_by="user_message",
            )

            loop = asyncio.get_running_loop()

            try:
                # run_turn() is blocking by design — the model call and the
                # calendar reads must not run on the event loop. The whole turn
                # goes into ONE executor call so the tool cache and the fact
                # ledger, which are per-turn objects the loop creates and passes
                # down, live and die with it. (They were thread-locals once;
                # tools run in a worker pool and could not see them, so every
                # precondition failed closed. See tools/ledger.py.) The wait_for
                # is the backstop under the profile's own deadline (120s), not
                # the primary budget.
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: run_turn(request, self.conn)),
                    timeout=_EXECUTOR_TIMEOUT_S,
                )
            except TimeoutError:
                logger.warning(
                    f"on_message: the turn was still running after "
                    f"{_EXECUTOR_TIMEOUT_S}s — releasing the pipeline. "
                    f"text={text[:80]!r}"
                )
                fail_msg = "Sorry, sir — that request timed out on my end. Try again?"
                await update.message.reply_text(fail_msg)
                now_iso = datetime.now().isoformat()
                self.conn.execute(
                    "INSERT INTO conversation_history (role, content, created_at) VALUES (?, ?, ?)",
                    ("user", text, now_iso),
                )
                self.conn.execute(
                    "INSERT INTO conversation_history (role, content, created_at) VALUES (?, ?, ?)",
                    ("assistant", fail_msg, now_iso),
                )
                self.conn.commit()
                return
            if result.tool_calls_made:
                logger.info(
                    f"Turn used {result.tool_calls_made} tool call(s) over "
                    f"{result.hops} hop(s), ended on {result.stopped_on}."
                )

            now_iso = datetime.now().isoformat()
            self.conn.execute(
                "INSERT INTO conversation_history (role, content, created_at) VALUES (?, ?, ?)",
                ("user", text, now_iso),
            )

            # EFFECTS RUN BEFORE THE MODEL'S OWN REPLY, and that ordering is
            # invariant 3 rather than a preference. A permission card has to
            # reach the user before anything that could editorialize on it, and
            # the model's reply is exactly such a thing. effects/runner.py puts
            # the card first WITHIN the batch; this ordering puts the whole
            # batch ahead of the prose.
            #
            # Blocking `requests` calls, so an executor. The turn's own
            # deadline has already been spent by this point, which is why this
            # is not inside it.
            card_sent = False
            if result.effects:
                report = await loop.run_in_executor(
                    None, lambda: effects_runner.run(result.effects, self)
                )
                card_sent = report.cards_sent > 0
                if not report.ok:
                    logger.warning(f"Effects partially failed: {report.failed}")

            # Failures are told apart by LLMResponse.error_kind, not by reaching
            # into the agent for a _last_error attribute. The network wording is
            # load-bearing: a blocked network has to be distinguishable from a
            # generic failure, here and later in the dashboard.
            if result.error_kind != "none":
                if result.error_kind == "rate_limit":
                    msg = "I'm being rate limited, sir. Try again in a moment."
                elif result.error_kind == "transient":
                    # Distinct from rate_limit on purpose: nothing the user did
                    # caused this and nothing they can do fixes it. The
                    # dispatcher already retried before we got here.
                    msg = "The model service is having trouble on its end, sir. Try again shortly."
                elif result.error_kind == "network":
                    msg = "I can't reach the model from this network."
                else:
                    msg = f"LLM error, sir: {result.error_message}"
                logger.warning(f"LLM {result.error_kind} — sending to user: {msg}")
                await update.message.reply_text(msg)
                assistant_log = msg
            elif result.text:
                await update.message.reply_text(result.text)
                assistant_log = result.text
            elif card_sent:
                # A card went out and the model said nothing after it. That is
                # the CORRECT shape for a gated write, not an empty response:
                # the card is the reply, and anything appended to it would be
                # the preamble invariant 3 forbids. The old code answered this
                # case with "the model returned no text", which is how the
                # Phase II turn ended in an empty markdown fence.
                assistant_log = "[permission card sent]"
            else:
                msg = "Sorry, sir — the model returned no text. Try again?"
                logger.warning(f"Empty LLM response — sending to user: {msg}")
                await update.message.reply_text(msg)
                assistant_log = msg

            self.conn.execute(
                "INSERT INTO conversation_history (role, content, created_at) VALUES (?, ?, ?)",
                ("assistant", assistant_log, now_iso),
            )
            self.conn.commit()

    async def on_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Entry point for photos and PDF documents → calendar event extraction.
        Same semaphore and pause gate as text, so media queues in arrival order
        with everything else."""
        async with _semaphore:
            msg = update.message
            if msg is None:
                return
            caption = (msg.caption or "").strip() or None
            if self._pause_active(f"media, caption: {(caption or '')[:80]}"):
                return

            try:
                if msg.photo:
                    tg_file = await msg.photo[-1].get_file()
                    file_bytes = bytes(await tg_file.download_as_bytearray())
                    mime_type = "image/jpeg"
                elif msg.document and msg.document.mime_type == "application/pdf":
                    tg_file = await msg.document.get_file()
                    file_bytes = bytes(await tg_file.download_as_bytearray())
                    mime_type = "application/pdf"
                else:
                    return
            except Exception as e:
                # Most common cause: Telegram bots can only download files ≤20 MB.
                logger.error(f"Media download failed: {e}")
                await msg.reply_text(
                    "Couldn't download that file, sir — Telegram only lets me "
                    "fetch files up to 20 MB."
                )
                return

            kind = "PDF" if mime_type == "application/pdf" else "photo"
            state.set_many(self.conn, {
                "last_message_at":      datetime.now().isoformat(),
                "last_message_preview": f"[{kind}] {caption or ''}"[:80].strip(),
            })
            logger.info(f"Media: {kind}, {len(file_bytes)} bytes, caption={caption!r}")

            loop = asyncio.get_running_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None, self.agent.on_media, file_bytes, mime_type, caption
                    ),
                    timeout=_EXECUTOR_TIMEOUT_S,
                )
            except TimeoutError:
                logger.warning(
                    f"on_media: extraction still running after "
                    f"{_EXECUTOR_TIMEOUT_S}s — releasing the pipeline."
                )
                await msg.reply_text(
                    "Sorry, sir — reading that file timed out on my end. "
                    "Try sending it again?"
                )

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
