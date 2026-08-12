"""
channels/telegram.py
Telegram interface for Friday.

Transport, and only transport, since the second channel arrived. Inbound
handlers pull bytes off a PTB update and hand them to
channels/conversation.py, which owns the gate and everything above it.
Outbound is sync requests.post — fast, safe to call from any context, and run
in an executor by whoever calls it.

The one place there is real logic here is send_permission_request: the buttons
are Telegram's business. The card's text is not.
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
from channels import conversation
from channels.base import Channel

logger = logging.getLogger("friday.telegram")

_API_BASE = "https://api.telegram.org/bot{token}/{method}"

# Features handled here:
#   • on_message  — transport. Pulls the text off the update and hands it to
#     channels/conversation.py::handle(), which owns the gate, the pause check,
#     the history window and the reply. Everything there is channel-agnostic
#     and the dashboard runs the identical path.
#   • on_callback — inline-button taps for the approval-gate cards. Resolves
#     by key through effects/pending.py; the row names the tool to run.
#   • send / send_permission_request — synchronous outbound helpers.
# THE GATE MOVED, IT DID NOT GO AWAY. channels/conversation.py::TURN_GATE is
# the one Semaphore(1) for the process, acquired at the top of handle() before
# any SQLite query, any context assembly, any model call. It is not here any
# more because there are two channels now, and a gate that belongs to one of
# them serializes that one against itself while letting the two interleave
# against the same conversation_history. The executor ceiling moved with it.


class TelegramHandler(Channel):
    """Telegram, as a channel. Subclassed explicitly rather than left to duck
    typing: the base's methods are abstract, so forgetting one fails here at
    construction instead of at 2am when the first card needs sending."""

    name = "telegram"

    def __init__(self, config: dict, agent, conn: sqlite3.Connection):
        tg = config.get("telegram", config)  # accept full config or telegram sub-dict
        self.config       = config
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
        """The approval gate card.

        THE DRAFT IS SENT VERBATIM. Nothing is prepended, appended or wrapped
        around it — invariant 3 says nothing may delay, editorialize on, or
        bury a card, and a banner above the proposal is the smallest possible
        version of burying it. This used to send "Friday\n\n" + the draft,
        which put a word the user did not need above the thing they were being
        asked to approve.

        No Edit button. There was one; its callback fell through the verb check
        below and did nothing, after answering the tap — so the button lit up,
        acknowledged, and silently ignored the user. A button that does that is
        worse than no button. The dashboard keeps full edit
        (/api/pending-approvals/{id}/edit); Telegram parity is step 5's if it
        is wanted.
        """
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{pending_key}"),
            InlineKeyboardButton("❌ Cancel",  callback_data=f"cancel:{pending_key}"),
        ]])
        url = _API_BASE.format(token=self.bot_token, method="sendMessage")
        try:
            r = requests.post(url, json={
                "chat_id": self.chat_id,
                "text": draft,
                "reply_markup": keyboard.to_dict(),
            }, timeout=10)
            if r.status_code == 200:
                logger.info(f"Card sent ({pending_key}): {draft[:80]}")
                return True
            logger.error(f"send_permission_request failed {r.status_code}: {r.text[:120]}")
            return False
        except Exception as e:
            logger.error(f"send_permission_request error: {e}")
            return False

    def notify(self, title: str, text: str) -> bool:
        """Interrupt. On Telegram this is just a message — the transport IS a
        push notification, and there is no second, louder door to knock on.

        Sent as title then body, LITERALLY, with no persona and no quip
        (invariant 6). The dashboard's implementation is the one that has real
        work to do here; this one exists so the contract is not a special case
        with a hole in it.
        """
        body = f"{title}\n{text}" if title and text else (title or text)
        return self.send(body)

    # ── Inbound (async) ───────────────────────────────────────────────────────

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Entry point for all text messages. TRANSPORT ONLY.

        Everything that used to be here — the gate, the pause check, the
        history window, the request, the executor hop, the effects, the reply
        selection, the history writes — is in channels/conversation.py, because
        every line of it except `update.message` is channel-agnostic and the
        dashboard needs all of it. Copying it would have been a second chat
        implementation, which invariant 9 forbids in those words.

        THE GATE IS STILL FIRST AND IT IS STILL ONE. handle() acquires it
        before any SQLite query, any context assembly, any model call — the
        rule has not been relaxed, it has been moved somewhere a second channel
        cannot forget it. A private Semaphore(1) in the dashboard would
        serialize the dashboard against itself while letting a dashboard turn
        and a Telegram turn interleave against the same conversation_history.

        The reply goes out through self.send() rather than reply_text(). That
        looked like transport and was not: it was the only path prose took to
        the user, and it is why the split was invisible for so long.
        """
        text = (update.message.text or "").strip()
        if not text:
            return
        await conversation.handle(text, self, self.conn, self.config)

    async def on_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Entry point for photos and PDF documents → calendar event extraction.
        Same gate and pause check as text — channels/conversation.py owns both
        — so media queues in arrival order with everything else.

        NOT routed through conversation.handle(): this path reaches no model
        (agent/core.py is media intake only since the teardown) and produces no
        turn. It shares the gate because it shares the pipeline, not because it
        is a conversation."""
        async with conversation.TURN_GATE:
            msg = update.message
            if msg is None:
                return
            caption = (msg.caption or "").strip() or None
            if conversation.pause_active(
                    self.conn, f"media, caption: {(caption or '')[:80]}"):
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
                    timeout=conversation.EXECUTOR_TIMEOUT_S,
                )
            except TimeoutError:
                logger.warning(
                    f"on_media: extraction still running after "
                    f"{conversation.EXECUTOR_TIMEOUT_S}s — releasing the pipeline."
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
            logger.warning(f"Unhandled callback verb {verb!r} — ignoring.")
            return
        row = self.conn.execute(
            "SELECT action_type FROM pending_actions WHERE id = ?", (key,),
        ).fetchone()
        if not row:
            return
        action_type = row[0]
        loop = asyncio.get_running_loop()

        # Everything a card can propose is a tool call now, so this resolves by
        # KEY rather than by hardcoding what each action type means. The tool
        # name and its arguments are on the row; effects/pending.py runs them
        # through the same executor a turn uses.
        #
        # Blocking — a calendar write is a subprocess — so it goes to an
        # executor. It does NOT take the message semaphore: a tap is the user
        # answering a question Friday already asked, and queueing it behind an
        # unrelated in-flight message would make the button feel broken.
        if action_type == "tool_call":
            from effects import pending
            fn = pending.confirm if verb == "confirm" else pending.cancel
            await loop.run_in_executor(None, fn, key, self.conn, self)
            return

        # Rows staged before step 4 by the old gated_write. Kept working so a
        # card already in someone's chat still resolves; nothing produces these
        # any more.
        if action_type == "calendar_add":
            from actions import calendar as cal_action
            fn = cal_action.confirm_pending if verb == "confirm" else cal_action.cancel_pending
            await loop.run_in_executor(None, fn, key, self.conn, self)
