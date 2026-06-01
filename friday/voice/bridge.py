"""Telegram bridge — talks to the Friday bot AS the user account via Telethon.

Why a user-account client (not the bot API): a Telegram bot cannot impersonate
a user, so the voice transcription must originate from the user's account to
trigger Friday's `on_message` handler.

First-run requirement:
    Register a Telegram app at https://my.telegram.org → "API development tools"
    Paste the issued `api_id` (int) and `api_hash` (str) into friday_config.yaml
    under `telegram.api_id` and `telegram.api_hash`.

First-run auth: the first time `connect()` runs it prompts in the terminal for
the user's phone number and the Telegram-sent code. The session is saved to
`voice/friday_voice.session` and subsequent runs need no interaction.

`TelegramBridge` owns its own asyncio loop in a background thread; the
synchronous `send_and_wait` is the integration point for the rest of voice/.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events

_LOGGER = logging.getLogger(__name__)

SESSION_PATH = Path(__file__).resolve().parent / "friday_voice.session"


class TelegramBridge:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        bot_token: str,
        session_path: Path = SESSION_PATH,
    ) -> None:
        if not (api_id and api_hash and bot_token):
            raise ValueError("api_id, api_hash, bot_token are all required")
        self.api_id = api_id
        self.api_hash = api_hash
        self.bot_token = bot_token
        # The bot's user ID is the integer prefix of its token.
        try:
            self.bot_user_id = int(bot_token.split(":")[0])
        except (ValueError, IndexError) as e:
            raise ValueError(f"malformed bot_token: {e}") from e

        # Strip ".session" if user typed it — Telethon adds it back.
        session_name = str(session_path)
        if session_name.endswith(".session"):
            session_name = session_name[: -len(".session")]
        self._session_name = session_name

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._client: Optional[TelegramClient] = None
        self._bot_entity = None
        self._ready = threading.Event()
        self._shutdown = threading.Event()

    # ---------- public sync API ----------

    def connect(self) -> None:
        """Start the asyncio loop in a worker thread and run the Telethon
        client login. Blocks until the client is authenticated and the bot
        entity is resolved."""
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="telegram-bridge-loop", daemon=True
        )
        self._loop_thread.start()
        fut = asyncio.run_coroutine_threadsafe(self._async_connect(), self._loop)
        fut.result()  # surfaces exceptions

    def disconnect(self) -> None:
        if self._loop is None:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(self._async_disconnect(), self._loop)
            fut.result(timeout=5)
        except Exception as e:
            _LOGGER.warning("bridge disconnect error: %s", e)
        finally:
            self._shutdown.set()
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

    def send_and_wait(self, text: str, timeout: int = 30) -> Optional[str]:
        """Send `text` to the Friday bot AS the user. Wait up to `timeout`
        seconds for the bot's reply. Returns the reply text or None on timeout."""
        if self._loop is None or self._client is None:
            raise RuntimeError("bridge not connected")
        fut = asyncio.run_coroutine_threadsafe(
            self._async_send_and_wait(text, timeout), self._loop
        )
        try:
            return fut.result(timeout=timeout + 5)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            _LOGGER.exception("send_and_wait failed: %s", e)
            return None

    # ---------- internals ----------

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _async_connect(self) -> None:
        self._client = TelegramClient(self._session_name, self.api_id, self.api_hash)
        # `start()` triggers the interactive auth flow if no session exists.
        # On first run this prompts in the terminal for phone + code.
        await self._client.start()
        _LOGGER.info("Telethon client authenticated")

        # Resolve the bot once and cache the entity.
        try:
            self._bot_entity = await self._client.get_entity(self.bot_user_id)
        except Exception as e:
            raise RuntimeError(
                f"could not resolve bot entity (id={self.bot_user_id}): {e}. "
                "Make sure you've started a chat with the bot at least once."
            ) from e
        _LOGGER.info("bot entity resolved: %s", getattr(self._bot_entity, "username", self.bot_user_id))
        self._ready.set()

    async def _async_disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                _LOGGER.debug("client disconnect: %s", e)

    async def _async_send_and_wait(self, text: str, timeout: int) -> Optional[str]:
        assert self._client is not None
        reply_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

        # Use NewMessage event to catch the reply as it arrives — beats polling.
        @self._client.on(events.NewMessage(from_users=self.bot_user_id))
        async def _on_bot_reply(event):
            if reply_future.done():
                return
            try:
                reply_future.set_result(event.message.message or "")
            except Exception:
                pass

        try:
            await self._client.send_message(self._bot_entity, text)
            _LOGGER.info("voice → bot: %s", text[:80])
            try:
                return await asyncio.wait_for(reply_future, timeout=timeout)
            except asyncio.TimeoutError:
                _LOGGER.warning("no bot reply after %ds", timeout)
                return None
        finally:
            # Remove the handler so we don't accumulate one per call.
            self._client.remove_event_handler(_on_bot_reply)


if __name__ == "__main__":
    # CLI: smoke test the bridge. Reads config, prompts auth, sends a ping.
    import logging
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import load, telegram_credentials_present

    logging.basicConfig(level=logging.INFO)
    cfg = load()
    if not telegram_credentials_present(cfg):
        print(
            "Telegram credentials missing. Add telegram.api_id and "
            "telegram.api_hash to friday_config.yaml. See bridge.py docstring."
        )
        sys.exit(1)

    bridge = TelegramBridge(cfg.telegram_api_id, cfg.telegram_api_hash, cfg.telegram_bot_token)
    bridge.connect()
    try:
        reply = bridge.send_and_wait("voice bridge smoke test, please respond", timeout=30)
        print("REPLY:", reply)
    finally:
        bridge.disconnect()
