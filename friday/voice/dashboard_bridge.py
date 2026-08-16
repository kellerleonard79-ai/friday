"""Direct-to-dashboard bridge — the low-latency alternative to bridge.py.

WHY THIS EXISTS. TelegramBridge round-trips every transcript through
Telegram's servers twice — send, then wait for the bot's reply — with PTB's
getUpdates poll interval sitting in between the reply landing and Friday's own
handler seeing it. POST /api/chat calls channels/conversation.py::handle() in
the SAME process, over loopback: the identical turn, minus both Telegram hops
and the poll gap. Per dashboard/server.py's own docstring on that route, this
is not a shortcut around Friday's core — the dashboard is a first-class
channel (Phase III step 5) and /api/chat is "the same pipeline Telegram
runs," just reached without leaving the machine.

It also sidesteps a real bug the old path had: Telegram is blocked outbound
on school Wi-Fi roughly seven hours a day (see CLAUDE.md's Channel Layer
section). During that window the Telegram bridge would sit for the full
`response_timeout_s` on every single PTT press despite Friday running right
there on the same Mac the whole time. Loopback HTTP does not care what the
Wi-Fi is doing.

This module still never imports anything under friday/'s package tree — see
listen.py's own docstring on that rule. It only speaks HTTP to the dashboard,
exactly the way menubar.py and mac_app.py already do from outside voice/.

`BridgeResult` / `Outcome` are reused from bridge.py so listen.py's session
state machine (the failure-cue table, the `.ok` / `.reply` checks) needs no
branching on which bridge produced the result. `DISCONNECTED` and
`EMPTY_TEXT` are the only bridge.py outcomes this module doesn't itself
produce — there is no persistent connection here to drop mid-wait.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

import requests

from bridge import BridgeResult, Outcome

_LOGGER = logging.getLogger(__name__)

# Loopback only, matching mac_app.py / menubar.py's own hardcoded constant —
# the dashboard binds here today and the bind-beyond-loopback work (Tailscale)
# is explicitly not done yet per CLAUDE.md, so there is nothing else to point
# at.
DEFAULT_BASE_URL = "http://127.0.0.1:5174"


class DashboardBridge:
    """Posts a transcript to /api/chat and waits for the turn to finish."""

    def __init__(self, auth_token: str, base_url: str = DEFAULT_BASE_URL) -> None:
        if not auth_token:
            raise ValueError(
                "dashboard.auth_token is required — it is generated on "
                "Friday's first boot and lives in friday_config.yaml."
            )
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers["X-Friday-Token"] = auth_token

    # ---------- public sync API (mirrors TelegramBridge's shape) ----------

    def connect(self) -> None:
        """Best-effort reachability probe at boot.

        Not required — send_and_wait works cold — but a boot-time warning
        beats a silent timeout on the user's first PTT press of the session."""
        try:
            r = self._session.get(f"{self.base_url}/api/status", timeout=5)
            r.raise_for_status()
            _LOGGER.info("dashboard bridge: reachable at %s", self.base_url)
        except Exception as e:
            _LOGGER.warning(
                "dashboard bridge: could not reach %s at boot (%s) — will "
                "retry on the first PTT press.", self.base_url, e,
            )

    def disconnect(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    def send_and_wait(
        self,
        text: str,
        timeout: int = 30,
        on_slow: Optional[Callable[[], None]] = None,
        slow_after_s: int = 0,
    ) -> BridgeResult:
        """POST `text` to /api/chat and wait up to `timeout` seconds. Always
        returns a BridgeResult — never raises. `on_slow` fires once, from this
        thread, if `slow_after_s` passes before the response arrives — the
        request itself runs on a worker thread so this loop can poll for that
        without blocking on `requests`."""
        if not text or not text.strip():
            _LOGGER.warning("send_and_wait: refusing to send empty/whitespace text")
            return BridgeResult(Outcome.EMPTY_TEXT, detail="empty text")

        _LOGGER.info("send_and_wait: dispatching (%d chars, timeout=%ds)", len(text), timeout)

        box: dict[str, Any] = {}

        def _do_request() -> None:
            try:
                box["response"] = self._session.post(
                    f"{self.base_url}/api/chat", json={"text": text}, timeout=timeout,
                )
            except Exception as e:
                box["error"] = e

        t0 = time.monotonic()
        worker = threading.Thread(target=_do_request, name="dashboard-bridge-post", daemon=True)
        worker.start()

        slow_fired = False
        while worker.is_alive():
            worker.join(timeout=0.25)
            if (
                not slow_fired and on_slow and slow_after_s > 0
                and time.monotonic() - t0 >= slow_after_s
            ):
                slow_fired = True
                try:
                    on_slow()
                except Exception as e:
                    _LOGGER.warning("on_slow callback raised: %s", e)

        if "error" in box:
            e = box["error"]
            if isinstance(e, requests.exceptions.Timeout):
                _LOGGER.warning("send_and_wait: timed out after %ds", timeout)
                return BridgeResult(Outcome.TIMEOUT, detail=f"no reply within {timeout}s")
            if isinstance(e, requests.exceptions.ConnectionError):
                _LOGGER.error("send_and_wait: dashboard unreachable: %s", e)
                return BridgeResult(Outcome.NOT_CONNECTED, detail=str(e))
            _LOGGER.exception(
                "send_and_wait: request raised %s: %s", type(e).__name__, e,
            )
            return BridgeResult(
                Outcome.INTERNAL_ERROR, detail=f"{type(e).__name__}: {e}"
            )

        resp = box["response"]
        if resp.status_code == 401:
            _LOGGER.error("send_and_wait: dashboard rejected the auth token")
            return BridgeResult(
                Outcome.SEND_FAILED,
                detail="401 unauthorized — check voice's copy of dashboard.auth_token",
            )
        if resp.status_code != 200:
            _LOGGER.error(
                "send_and_wait: dashboard returned %d: %s",
                resp.status_code, resp.text[:200],
            )
            return BridgeResult(Outcome.SEND_FAILED, detail=f"HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as e:
            return BridgeResult(Outcome.INTERNAL_ERROR, detail=f"bad JSON reply: {e}")

        if data.get("paused"):
            _LOGGER.info("send_and_wait: Friday is paused — message was dropped")
            return BridgeResult(Outcome.OK, reply="I'm paused right now, sir.")

        # channel.events, in the order conversation.handle() produced them —
        # a card first if one went out (invariant 3), then the model's own
        # reply if nothing suppressed it. Concatenating them reproduces
        # exactly what a Telegram session would have received as bot
        # messages, without TelegramBridge's "only the first message" limit.
        reply = "\n\n".join(
            ev.get("text", "") for ev in data.get("events", [])
            if ev.get("kind") in ("message", "card") and ev.get("text")
        )
        _LOGGER.info("send_and_wait: reply received (%d chars)", len(reply))
        return BridgeResult(Outcome.OK, reply=reply)


if __name__ == "__main__":
    # CLI smoke test: reads config, pings /api/status, sends a ping turn.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from config import load

    logging.basicConfig(level=logging.INFO)
    cfg = load()
    if not cfg.dashboard_auth_token:
        print(
            "dashboard.auth_token missing from friday_config.yaml — Friday "
            "generates one on first boot. Start the daemon once, then retry."
        )
        sys.exit(1)

    bridge = DashboardBridge(auth_token=cfg.dashboard_auth_token)
    bridge.connect()
    result = bridge.send_and_wait("dashboard bridge smoke test, please respond", timeout=30)
    print("OUTCOME:", result.outcome.value, "| reached_friday:", result.reached_friday)
    print("REPLY:", result.reply)
    if result.detail:
        print("DETAIL:", result.detail)
    bridge.disconnect()
