"""
menubar.py
F.R.I.D.A.Y. menu bar app — standalone rumps app, never imported by friday.py.

Reads live status from the dashboard server's /api/status endpoint. Renders a
custom orange "F.R.I.D.A.Y." icon (no title text). Provides a Pause for...
submenu with timed pauses; the dashboard server stores `paused_until` in
system_state, the telegram pause guard auto-clears it on the next message,
and this menubar's tick auto-resumes proactively when the deadline lapses.
"""

import os
import subprocess
import sys
from datetime import datetime, time, timedelta

import requests
import rumps
from AppKit import NSApplication, NSImage

import menubar_icon

_HERE   = os.path.dirname(os.path.abspath(__file__))
_PYTHON = sys.executable
_LOG    = os.path.join(_HERE, "logs", "friday.log")

_DASH_URL = "http://127.0.0.1:5174"
_STATUS   = f"{_DASH_URL}/api/status"
_PAUSE    = f"{_DASH_URL}/api/friday/pause"
_BRIEF    = f"{_DASH_URL}/api/friday/brief"


def _friday_running_proc() -> bool:
    return subprocess.run(["pgrep", "-f", "friday.py"], capture_output=True).returncode == 0


def _status_snapshot() -> dict:
    try:
        r = requests.get(_STATUS, timeout=1.5)
        if r.status_code != 200:
            return {"state": "error" if _friday_running_proc() else "offline"}
        d = r.json()
        if d.get("status") == "running" and d.get("paused"):
            return {"state": "paused", **d}
        if d.get("status") == "running":
            return {"state": "online", **d}
        return {"state": "offline", **d}
    except Exception:
        return {"state": "error" if _friday_running_proc() else "offline"}


def _fmt_clock(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return "—"


def _fmt_int(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "0"


class FridayMenuBar(rumps.App):
    def __init__(self):
        icons = menubar_icon.ensure_icons()
        self._icons = icons
        self._user_icon_mtime = self._current_user_mtime()
        # title="" so only the icon shows. icon set below per state.
        super().__init__("Friday", title="", icon=icons["offline"],
                         quit_button=None, template=False)
        # Replace the Python "rocket" Dock icon with the user PNG (uncropped).
        self._apply_dock_icon()

        # Non-clickable status header (set_callback(None) renders as disabled)
        self._status_row = rumps.MenuItem("Connecting…")
        self._status_row.set_callback(None)

        self._brief      = rumps.MenuItem("Brief Me Now", callback=self.brief_me)
        self._pause      = rumps.MenuItem("Pause Friday", callback=self.toggle_pause)

        # Pause for... submenu
        self._pause_for  = rumps.MenuItem("Pause For…")
        self._pause_15m  = rumps.MenuItem("15 minutes",        callback=lambda s: self._pause_for_seconds(15 * 60))
        self._pause_1h   = rumps.MenuItem("1 hour",            callback=lambda s: self._pause_for_seconds(60 * 60))
        self._pause_8am  = rumps.MenuItem("Until 8 AM",        callback=lambda s: self._pause_until_8am())
        self._pause_for.update([self._pause_15m, self._pause_1h, self._pause_8am])

        self._dashboard  = rumps.MenuItem("Open Dashboard", callback=self.open_dashboard)
        self._logs       = rumps.MenuItem("Open Logs",      callback=self.open_logs)
        self._quit       = rumps.MenuItem("Quit Friday Bar", callback=rumps.quit_application)

        self.menu = [
            self._status_row,
            None,
            self._brief,
            self._pause,
            self._pause_for,
            None,
            self._dashboard,
            self._logs,
            None,
            self._quit,
        ]

        self._last_state = None
        self._tick(None)
        rumps.Timer(self._tick, 10).start()

    # ── Tick ──────────────────────────────────────────────────────────────

    @staticmethod
    def _current_user_mtime() -> float | None:
        try:
            return menubar_icon.USER_ICON_PATH.stat().st_mtime
        except FileNotFoundError:
            return None

    def _maybe_refresh_icons(self) -> None:
        """Re-pick icons if the user dropped/updated a custom PNG."""
        m = self._current_user_mtime()
        if m != self._user_icon_mtime:
            self._user_icon_mtime = m
            self._icons = menubar_icon.regenerate()
            # Force re-apply on next state comparison.
            self._last_state = None
            self._apply_dock_icon()

    def _apply_dock_icon(self) -> None:
        """Replace the default Python rocket Dock icon with the user PNG,
        center-cropped to a circle (transparent corners)."""
        src = menubar_icon.USER_ICON_PATH
        if not src.exists():
            return
        img = NSImage.alloc().initWithContentsOfFile_(str(src))
        if img is not None and img.size().width > 0:
            NSApplication.sharedApplication().setApplicationIconImage_(
                menubar_icon.circular_crop(img)
            )

    def _tick(self, _):
        self._maybe_refresh_icons()
        snap = _status_snapshot()
        st = snap["state"]

        # Proactive auto-resume if the timed-pause deadline has elapsed.
        if st == "paused":
            until = snap.get("paused_until")
            if until:
                try:
                    if datetime.fromisoformat(until) <= datetime.now():
                        self._call_pause(False)
                        snap = _status_snapshot()
                        st = snap["state"]
                except ValueError:
                    pass

        # Icon swap on state change.
        if st != self._last_state:
            self._last_state = st
            self.icon = self._icons.get(st, self._icons["offline"])
            self._pause.title = "Resume Friday" if st == "paused" else "Pause Friday"
            self._pause_for.title = "Resume Friday" if st == "paused" else "Pause For…"

        # Status row: state line + a compact stats line.
        last_msg  = _fmt_clock(snap.get("last_message_at"))
        tin       = _fmt_int(snap.get("tokens_in"))
        tout      = _fmt_int(snap.get("tokens_out"))
        calls     = _fmt_int(snap.get("think_calls"))
        if st == "paused" and snap.get("paused_until"):
            try:
                until_dt = datetime.fromisoformat(snap["paused_until"])
                self._status_row.title = f"Paused · resumes {until_dt.strftime('%H:%M')}"
            except ValueError:
                self._status_row.title = "Paused"
        else:
            label = {"online": "Online", "paused": "Paused",
                     "offline": "Offline", "error": "Error"}.get(st, st)
            self._status_row.title = (
                f"{label} · last msg {last_msg} · "
                f"{calls} calls · {tin}/{tout} tok"
            )

    # ── Callbacks ─────────────────────────────────────────────────────────

    def brief_me(self, _):
        try:
            r = requests.post(_BRIEF, timeout=5)
            if r.status_code != 200:
                rumps.alert("Friday", f"Brief failed: HTTP {r.status_code}")
        except Exception as e:
            rumps.alert("Friday", f"Brief failed: {e}")

    def toggle_pause(self, _):
        snap = _status_snapshot()
        self._call_pause(snap["state"] != "paused")
        self._tick(None)

    def _pause_for_seconds(self, seconds: int) -> None:
        until = datetime.now() + timedelta(seconds=seconds)
        self._call_pause(True, until.isoformat(timespec="seconds"))
        self._tick(None)

    def _pause_until_8am(self) -> None:
        now = datetime.now()
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        self._call_pause(True, target.isoformat(timespec="seconds"))
        self._tick(None)

    def _call_pause(self, paused: bool, until: str | None = None) -> None:
        body = {"paused": paused}
        if until:
            body["until"] = until
        try:
            r = requests.post(_PAUSE, json=body, timeout=5)
            if r.status_code != 200:
                rumps.alert("Friday", f"Pause failed: HTTP {r.status_code}")
        except Exception as e:
            rumps.alert("Friday", f"Pause failed: {e}")

    def open_dashboard(self, _):
        subprocess.Popen(["open", _DASH_URL], start_new_session=True)

    def open_logs(self, _):
        if os.path.exists(_LOG):
            subprocess.Popen(["open", _LOG], start_new_session=True)
        else:
            rumps.alert("Friday", f"Log not found: {_LOG}")


if __name__ == "__main__":
    FridayMenuBar().run()
