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
It returns a `BridgeResult` rather than a bare reply string so callers can
tell a slow answer apart from an undelivered one — see `Outcome`.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from telethon import TelegramClient, events
from telethon.sessions import StringSession

_LOGGER = logging.getLogger(__name__)

# Ceiling on a pre-send reconnect. Short on purpose: the user is standing there
# with the PTT key just released, so failing fast to "I couldn't reach Friday"
# beats a long silence.
_RECONNECT_TIMEOUT_S = 10


class Outcome(str, Enum):
    """Why a send_and_wait ended the way it did.

    The distinction that matters to a caller is `BridgeResult.reached_friday`:
    a timeout means the message IS with Friday and the answer is merely late
    (it will still land in Telegram), whereas a send failure means nothing was
    delivered at all. Collapsing those two into a bare None is what made voice
    announce "I couldn't reach Friday" about a briefing that arrived four
    seconds later."""

    OK = "ok"                        # reply received
    TIMEOUT = "timeout"              # sent, no reply inside the window
    DISCONNECTED = "disconnected"    # sent, connection dropped while waiting
    SEND_FAILED = "send_failed"      # Telegram rejected the send
    NOT_CONNECTED = "not_connected"  # bridge was never up
    EMPTY_TEXT = "empty_text"        # refused before sending
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class BridgeResult:
    outcome: Outcome
    reply: Optional[str] = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK

    @property
    def reached_friday(self) -> bool:
        """True if the message was handed to Telegram, regardless of whether a
        reply came back. Callers use this to decide between "still working" and
        "couldn't reach" phrasing."""
        return self.outcome in (Outcome.OK, Outcome.TIMEOUT, Outcome.DISCONNECTED)


class TelegramBridge:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        bot_token: str,
        session_string: Optional[str] = None,
    ) -> None:
        """Initialize the bridge.

        `session_string` is an opaque base64-ish blob produced by Telethon's
        StringSession.save(). Pass the saved value (from friday_config.yaml's
        telegram.telethon_session) on subsequent runs to skip re-auth. Pass
        None / empty on first run; after connect() succeeds, read the newly
        generated value via get_session_string() and persist it.

        StringSession avoids the SQLite-backed file session, which was prone
        to OperationalError: database is locked when multiple Telethon
        components (update loop, keepalive, reply handler) raced or when
        two listener processes shared a session file."""
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

        self._session_string_in: str = session_string or ""
        self._session_string_out: Optional[str] = None

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

    def send_and_wait(
        self,
        text: str,
        timeout: int = 30,
        on_slow: Optional[Callable[[], None]] = None,
        slow_after_s: int = 0,
    ) -> BridgeResult:
        """Send `text` to the Friday bot AS the user and wait up to `timeout`
        seconds for the reply. Always returns a BridgeResult — never raises —
        carrying both the reply (if any) and the reason it ended. Every failure
        path is logged at WARNING or higher so silent drops are impossible.

        `on_slow`, if given, is called once — on the bridge's loop thread —
        when `slow_after_s` seconds pass with the message sent and no reply
        yet. It exists so the caller can tell the user the request is still
        in flight instead of leaving them in silence; keep it fast and
        non-blocking. Exceptions from it are swallowed."""
        if self._loop is None or self._client is None:
            _LOGGER.error("send_and_wait: bridge not connected (loop=%s client=%s)",
                          self._loop is not None, self._client is not None)
            return BridgeResult(Outcome.NOT_CONNECTED, detail="bridge not connected")
        if not text or not text.strip():
            _LOGGER.warning("send_and_wait: refusing to send empty/whitespace text")
            return BridgeResult(Outcome.EMPTY_TEXT, detail="empty text")

        _LOGGER.info("send_and_wait: dispatching (%d chars, timeout=%ds)", len(text), timeout)
        fut = asyncio.run_coroutine_threadsafe(
            self._async_send_and_wait(text, timeout, on_slow, slow_after_s), self._loop
        )
        # The inner coroutine may spend up to _RECONNECT_TIMEOUT_S reconnecting
        # before it even sends, so the outer wait has to allow for both legs.
        outer = timeout + _RECONNECT_TIMEOUT_S + 5
        try:
            result = fut.result(timeout=outer)
        except asyncio.TimeoutError:
            # The inner coroutine overran its own budget — it had already sent
            # by then in every path that can get here, so this is a slow reply,
            # not an unreachable bot.
            _LOGGER.warning("send_and_wait: outer future timed out after %ds", outer)
            return BridgeResult(
                Outcome.TIMEOUT, detail=f"outer future timed out after {outer}s"
            )
        except Exception as e:
            _LOGGER.exception("send_and_wait: future raised %s: %s", type(e).__name__, e)
            return BridgeResult(
                Outcome.INTERNAL_ERROR, detail=f"{type(e).__name__}: {e}"
            )

        if result.ok:
            _LOGGER.info("send_and_wait: reply received (%d chars)", len(result.reply or ""))
        else:
            _LOGGER.warning(
                "send_and_wait: no reply (outcome=%s, reached_friday=%s): %s",
                result.outcome.value, result.reached_friday, result.detail,
            )
        return result

    # ---------- internals ----------

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def get_session_string(self) -> Optional[str]:
        """After connect() succeeds, returns the StringSession blob to persist.
        Returns the same value that was passed in if nothing changed, or a
        freshly-generated value if this was a first-time auth. Callers should
        compare to the value they passed in and write back to config only if
        it changed, to avoid spurious file writes."""
        return self._session_string_out

    async def _async_connect(self) -> None:
        # StringSession with an empty string triggers the interactive auth
        # flow (phone + code). With a saved string it reauthenticates silently.
        self._client = TelegramClient(
            StringSession(self._session_string_in),
            self.api_id,
            self.api_hash,
        )
        # Under launchd there is no TTY, so Telethon's default phone/code
        # prompts call input() and hit EOFError → the LaunchAgent's KeepAlive
        # then respawns us in a tight loop. Detect headless context and raise
        # a clear error instead, so failure throttles cleanly and the user
        # knows exactly what to do (re-run bridge.py interactively).
        import sys as _sys

        headless = not _sys.stdin.isatty()

        def _no_tty(field: str):
            def _raise():
                raise RuntimeError(
                    f"voice: Telethon needs to {field} but no TTY is attached "
                    "(running under launchd?). Stop the LaunchAgent and run "
                    "`python voice/bridge.py` in a Terminal to re-auth; the "
                    "new session blob will persist back to friday_config.yaml."
                )
            return _raise

        if headless:
            await self._client.start(
                phone=_no_tty("ask for phone"),
                code_callback=_no_tty("ask for SMS code"),
                password=_no_tty("ask for 2FA password"),
            )
        else:
            await self._client.start()
        try:
            self._session_string_out = self._client.session.save()
        except Exception as e:
            _LOGGER.warning("could not snapshot session string: %s", e)
            self._session_string_out = None
        _LOGGER.info("Telethon client authenticated (StringSession)")

        # Warm the dialog cache so get_entity() can resolve the bot by numeric ID.
        # Without this, Telethon raises "Could not find input entity" even when
        # the user has an existing chat with the bot.
        await self._client.get_dialogs()

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

    async def _ensure_connected(self) -> bool:
        """Reconnect if the client has dropped. Returns False if it can't.

        Telethon's auto-reconnect gives up for good after `connection_retries`
        attempts ("Automatic reconnection failed 5 time(s)"), which a sleeping
        Mac or a Wi-Fi drop reaches easily. Once it does, `_user_connected`
        stays false and every send raises ConnectionError for the rest of the
        process's life — voice stayed dead until the LaunchAgent was restarted.
        connect() is cheap when already connected, so this runs before each send.
        """
        assert self._client is not None
        try:
            if self._client.is_connected():
                return True
        except Exception:
            pass  # treat an unreadable state as disconnected
        _LOGGER.warning("client disconnected — reconnecting")
        try:
            await asyncio.wait_for(self._client.connect(), timeout=_RECONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            _LOGGER.error("reconnect timed out after %ds", _RECONNECT_TIMEOUT_S)
            return False
        except Exception as e:
            _LOGGER.error("reconnect failed (%s): %s", type(e).__name__, e)
            return False
        # The session (and its entity cache) survives a reconnect, so the bot
        # entity is only re-resolved if we never got one.
        if self._bot_entity is None:
            try:
                self._bot_entity = await self._client.get_entity(self.bot_user_id)
            except Exception as e:
                _LOGGER.error("bot entity unresolved after reconnect: %s", e)
                return False
        _LOGGER.info("reconnected")
        return True

    async def _async_send_and_wait(
        self,
        text: str,
        timeout: int,
        on_slow: Optional[Callable[[], None]] = None,
        slow_after_s: int = 0,
    ) -> BridgeResult:
        assert self._client is not None
        if not await self._ensure_connected():
            return BridgeResult(
                Outcome.NOT_CONNECTED, detail="reconnect failed before send"
            )
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
            try:
                sent = await self._client.send_message(self._bot_entity, text)
            except Exception as e:
                _LOGGER.exception(
                    "send_message failed (%s): %s — text=%r",
                    type(e).__name__, e, text[:80],
                )
                return BridgeResult(
                    Outcome.SEND_FAILED, detail=f"{type(e).__name__}: {e}"
                )
            _LOGGER.info("voice → bot (msg_id=%s): %s",
                         getattr(sent, "id", "?"), text[:80])
            # Race the reply against connection loss. Without this, a Telethon
            # disconnect mid-wait makes us sit on a future that nothing will
            # ever resolve, burning the full timeout for no reason.
            try:
                reply = await self._wait_for_reply_or_disconnect(
                    reply_future, timeout, on_slow, slow_after_s
                )
            except asyncio.TimeoutError:
                # Sent fine — Friday is just slow. The answer will still arrive
                # in Telegram after we stop listening for it.
                _LOGGER.warning("no bot reply after %ds — Friday is slow or stuck", timeout)
                return BridgeResult(
                    Outcome.TIMEOUT, detail=f"no reply within {timeout}s"
                )
            if reply is None:
                # Disconnect path (logged inside helper). The send landed, so
                # this is distinct from never having reached Friday at all.
                return BridgeResult(
                    Outcome.DISCONNECTED, detail="client disconnected while waiting"
                )
            _LOGGER.info("bot → voice: %s", reply[:80] if reply else "<empty>")
            return BridgeResult(Outcome.OK, reply=reply)
        finally:
            # Remove the handler so we don't accumulate one per call.
            self._client.remove_event_handler(_on_bot_reply)

    async def _wait_for_reply_or_disconnect(
        self,
        reply_future: "asyncio.Future[str]",
        timeout: int,
        on_slow: Optional[Callable[[], None]] = None,
        slow_after_s: int = 0,
    ) -> Optional[str]:
        """Poll the connection state every 500 ms while waiting for the bot
        reply. Returns the reply on success, None if the client disconnects
        before the reply arrives, and raises asyncio.TimeoutError on full
        timeout. Fires `on_slow` once at `slow_after_s` if still waiting."""
        loop = asyncio.get_event_loop()
        start = loop.time()
        deadline = start + timeout
        slow_at = start + slow_after_s if (on_slow and slow_after_s > 0) else None
        tick = 0.5
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                return await asyncio.wait_for(
                    asyncio.shield(reply_future),
                    timeout=min(tick, remaining),
                )
            except asyncio.TimeoutError:
                if slow_at is not None and loop.time() >= slow_at:
                    slow_at = None  # fire once
                    _LOGGER.info(
                        "no reply after %ds — signalling slow response", slow_after_s
                    )
                    try:
                        on_slow()
                    except Exception as e:
                        _LOGGER.warning("on_slow callback raised: %s", e)
                # Did we lose the connection while waiting?
                connected = False
                try:
                    connected = bool(self._client and self._client.is_connected())
                except Exception:
                    connected = False
                if not connected:
                    _LOGGER.warning(
                        "Telethon client disconnected mid-wait — aborting reply wait",
                    )
                    return None
                # Still connected, keep polling.


if __name__ == "__main__":
    # CLI: smoke test the bridge. Reads config, prompts auth, sends a ping.
    import logging
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import load, persist_telethon_session, telegram_credentials_present

    logging.basicConfig(level=logging.INFO)
    cfg = load()
    if not telegram_credentials_present(cfg):
        print(
            "Telegram credentials missing. Add telegram.api_id and "
            "telegram.api_hash to friday_config.yaml. See bridge.py docstring."
        )
        sys.exit(1)

    bridge = TelegramBridge(
        cfg.telegram_api_id,
        cfg.telegram_api_hash,
        cfg.telegram_bot_token,
        session_string=cfg.telegram_telethon_session or None,
    )
    bridge.connect()
    new_session = bridge.get_session_string()
    if new_session and new_session != cfg.telegram_telethon_session:
        persist_telethon_session(new_session)
    try:
        result = bridge.send_and_wait("voice bridge smoke test, please respond", timeout=30)
        print("OUTCOME:", result.outcome.value, "| reached_friday:", result.reached_friday)
        print("REPLY:", result.reply)
        if result.detail:
            print("DETAIL:", result.detail)
    finally:
        bridge.disconnect()
