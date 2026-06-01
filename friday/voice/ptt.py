"""Push-to-talk key handler.

Wraps a `pynput.keyboard.Listener` so the PTT key (default: Right Option) can
hold-to-record from anywhere in the OS, not just when Friday is focused.

Macros macOS Accessibility permission — see README.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from pynput import keyboard

_LOGGER = logging.getLogger(__name__)


# Map config strings to pynput Key enum values.
_KEY_ALIASES = {
    "right_option": keyboard.Key.alt_r,
    "right_alt": keyboard.Key.alt_r,
    "left_option": keyboard.Key.alt_l,
    "left_alt": keyboard.Key.alt_l,
    "right_cmd": keyboard.Key.cmd_r,
    "left_cmd": keyboard.Key.cmd_l,
    "right_shift": keyboard.Key.shift_r,
    "left_shift": keyboard.Key.shift_l,
    "right_ctrl": keyboard.Key.ctrl_r,
    "left_ctrl": keyboard.Key.ctrl_l,
    "fn": keyboard.Key.cmd,  # macOS Fn isn't directly accessible; closest stand-in
}


def _resolve_key(name: str):
    name = (name or "").lower().strip()
    if name in _KEY_ALIASES:
        return _KEY_ALIASES[name]
    # Function keys (f1..f20)
    if name.startswith("f") and name[1:].isdigit():
        attr = name  # pynput exposes Key.f1..Key.f20
        k = getattr(keyboard.Key, attr, None)
        if k is not None:
            return k
    # Single character fallback
    if len(name) == 1:
        return keyboard.KeyCode.from_char(name)
    raise ValueError(f"unknown push_to_talk_key: {name!r}")


class PTTListener:
    """Global key listener. Fires `on_press_cb` once when the PTT key goes
    down (edge-triggered) and `on_release_cb` once when it comes up.

    `pressed_event` is a threading.Event that's set while the key is held —
    use this as the stop condition for `record_while_held`.
    """

    def __init__(
        self,
        key_name: str,
        on_press_cb: Callable[[], None],
        on_release_cb: Callable[[], None],
    ) -> None:
        self.key = _resolve_key(key_name)
        self.on_press_cb = on_press_cb
        self.on_release_cb = on_release_cb
        self.pressed_event = threading.Event()
        self.released_event = threading.Event()
        self.released_event.set()  # initial state: key is up

        self._held = False
        self._listener: Optional[keyboard.Listener] = None

    def _matches(self, k) -> bool:
        if k == self.key:
            return True
        # Listener may surface modifiers as Key.alt for either side on some
        # platforms; treat alt_r/alt_l as siblings only if explicitly configured.
        return False

    def _on_press(self, k) -> None:
        if not self._matches(k):
            return
        if self._held:
            return  # auto-repeat
        self._held = True
        self.pressed_event.set()
        self.released_event.clear()
        try:
            self.on_press_cb()
        except Exception as e:
            _LOGGER.exception("PTT on_press_cb raised: %s", e)

    def _on_release(self, k) -> None:
        if not self._matches(k):
            return
        if not self._held:
            return
        self._held = False
        self.pressed_event.clear()
        self.released_event.set()
        try:
            self.on_release_cb()
        except Exception as e:
            _LOGGER.exception("PTT on_release_cb raised: %s", e)

    def start(self) -> None:
        # suppress=False so other apps still see the key. We only observe.
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=False,
        )
        self._listener.start()
        _LOGGER.info("PTT listener started on key=%s", self.key)

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None


if __name__ == "__main__":
    import time
    logging.basicConfig(level=logging.INFO)
    p = PTTListener(
        "right_option",
        on_press_cb=lambda: print("PTT pressed"),
        on_release_cb=lambda: print("PTT released"),
    )
    p.start()
    print("hold Right Option (Right Alt). Ctrl-C to exit.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        p.stop()
