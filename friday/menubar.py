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
import threading
from datetime import datetime, time, timedelta
from pathlib import Path

import requests
import rumps
from AppKit import NSApplication, NSImage

import menubar_icon
import paths

# voice/listen.py creates this for the duration of every wake/PTT session
# (recording → transcription → bridge → TTS). Treat its presence as an
# authoritative "Friday is actively listening / handling a voice turn" signal.
LISTENING_FLAG = Path("/tmp/friday_listening")

_HERE   = os.path.dirname(os.path.abspath(__file__))
_PYTHON = sys.executable
# Via paths, not _HERE: inside a frozen .app the logs live in ~/.friday/logs
# while _HERE points at the read-only bundle.
_LOG    = str(paths.log_dir() / "friday.log")

_DASH_URL = "http://127.0.0.1:5174"
_STATUS   = f"{_DASH_URL}/api/status"
_PAUSE    = f"{_DASH_URL}/api/friday/pause"
_BRIEF    = f"{_DASH_URL}/api/friday/brief"
_VOICE_STATUS  = f"{_DASH_URL}/api/voice/status"
_VOICE_RESTART = f"{_DASH_URL}/api/voice/restart"
_VOICE_WAKE    = f"{_DASH_URL}/api/voice/wake"
_VOICE_LOG     = str(paths.log_dir() / "voice.err")


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


def _voice_snapshot() -> dict:
    """Best-effort snapshot of the voice LaunchAgent. Returns
    {state: 'online'|'listening'|'offline'|'error', session_present: bool}."""
    try:
        r = requests.get(_VOICE_STATUS, timeout=1.5)
        if r.status_code != 200:
            return {"state": "error"}
        d = r.json()
        if d.get("listening"):
            return {"state": "listening", **d}
        if d.get("agent_loaded"):
            return {"state": "online", **d}
        return {"state": "offline", **d}
    except Exception:
        return {"state": "error"}


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

        # Voice submenu: status header + wake mute toggle + restart + open log.
        self._voice         = rumps.MenuItem("Voice")
        self._voice_status  = rumps.MenuItem("Voice: connecting…")
        self._voice_status.set_callback(None)  # non-clickable header
        self._voice_wake    = rumps.MenuItem("Mute Wake Word", callback=self.toggle_wake)
        self._voice_restart = rumps.MenuItem("Restart Voice", callback=self.restart_voice)
        self._voice_logs    = rumps.MenuItem("Open Voice Logs", callback=self.open_voice_logs)
        self._voice.update([self._voice_status, None, self._voice_wake,
                            self._voice_restart, self._voice_logs])
        # Tracks wake_enabled so the menu title flips without restarting tick.
        self._wake_enabled: bool | None = None

        self._setup      = rumps.MenuItem("Run Setup Wizard", callback=self.run_setup)
        self._quit       = rumps.MenuItem("Quit Friday", callback=self.quit_friday)

        self.menu = [
            self._status_row,
            None,
            self._brief,
            self._pause,
            self._pause_for,
            None,
            self._voice,
            self._dashboard,
            self._logs,
            None,
            self._setup,
            self._quit,
        ]

        # Set by mac_app when this bar is running inside the packaged .app;
        # None when menubar.py was started on its own (LaunchAgent / source
        # checkout), where the core is somebody else's process to manage.
        self.supervisor = None

        self._last_state = None
        self._current_icon_key: str | None = None
        self._tick(None)
        rumps.Timer(self._tick, 10).start()
        # Fast poll just for the listening flag. Cheap (single stat() call) and
        # gives near-instant visual feedback when wake fires.
        rumps.Timer(self._listening_tick, 1).start()

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

    def _desired_icon_key(self) -> str:
        """Compute which cached icon should currently be displayed. Listening
        only overrides the `online` state — if Friday is paused/offline/error
        we keep the underlying state icon so the user isn't misled."""
        base = self._last_state or "offline"
        if base == "online" and LISTENING_FLAG.exists():
            return "listening"
        return base

    def _apply_icon(self) -> None:
        key = self._desired_icon_key()
        if key == self._current_icon_key:
            return
        self.icon = self._icons.get(key, self._icons.get("offline"))
        self._current_icon_key = key

    def _listening_tick(self, _):
        """Cheap 1s poll: just check the flag file and (maybe) swap the icon."""
        self._apply_icon()

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

        # Pause-menu titles follow the underlying state, not the listening
        # overlay — listening is transient, pause is a deliberate setting.
        if st != self._last_state:
            self._last_state = st
            self._pause.title = "Resume Friday" if st == "paused" else "Pause Friday"
            self._pause_for.title = "Resume Friday" if st == "paused" else "Pause For…"
        self._apply_icon()

        # Voice submenu header. Cheap call; folds into the 10 s tick.
        vsnap = _voice_snapshot()
        vlabel = {"online": "Online", "listening": "Listening…",
                  "offline": "Offline", "error": "Error"}.get(vsnap["state"], vsnap["state"])
        self._voice_status.title = f"Voice: {vlabel}"
        wake = vsnap.get("wake_enabled")
        if wake is not None and wake != self._wake_enabled:
            self._wake_enabled = bool(wake)
            self._voice_wake.title = (
                "Mute Wake Word" if self._wake_enabled else "Unmute Wake Word"
            )

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
            if st == "online" and LISTENING_FLAG.exists():
                label = "Listening…"
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

    def restart_voice(self, _):
        try:
            r = requests.post(_VOICE_RESTART, timeout=5)
            if r.status_code != 200:
                rumps.alert("Friday", f"Voice restart failed: HTTP {r.status_code}")
        except Exception as e:
            rumps.alert("Friday", f"Voice restart failed: {e}")

    def toggle_wake(self, _):
        """Flip voice.wake_enabled and kick the voice LaunchAgent. PTT keeps
        working in either mode — only the wake-word trigger is affected."""
        # Optimistic: assume currently-enabled if state unknown so the first
        # click reads as "mute".
        new_state = not (self._wake_enabled if self._wake_enabled is not None else True)
        try:
            r = requests.post(_VOICE_WAKE, json={"enabled": new_state}, timeout=5)
            if r.status_code != 200:
                rumps.alert("Friday", f"Wake toggle failed: HTTP {r.status_code}")
                return
        except Exception as e:
            rumps.alert("Friday", f"Wake toggle failed: {e}")
            return
        self._wake_enabled = new_state
        self._voice_wake.title = "Mute Wake Word" if new_state else "Unmute Wake Word"

    def open_voice_logs(self, _):
        if os.path.exists(_VOICE_LOG):
            subprocess.Popen(["open", _VOICE_LOG], start_new_session=True)
        else:
            rumps.alert("Friday", f"Voice log not found: {_VOICE_LOG}")

    def run_setup(self, _):
        """Re-run the wizard, then bounce the core so it reloads the config.

        Tkinter and rumps both want the main thread, so the wizard runs in its
        own process rather than in-process as the Windows tray does it. Waiting
        on that process happens on a worker thread — blocking here would wedge
        the whole menu bar for as long as the wizard is open.
        """
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--setup"]
        else:
            cmd = [_PYTHON, os.path.join(_HERE, "setup_wizard.py")]

        def worker():
            try:
                proc = subprocess.run(cmd, check=False)
            except Exception as e:
                # rumps.alert must be called on the main thread, so report to
                # stderr — which the LaunchAgent captures into ~/.friday/logs.
                print(f"Setup wizard failed to launch: {e}", file=sys.stderr)
                return
            if proc.returncode != 0:
                return  # cancelled or errored — leave the running core alone
            try:
                requests.post(f"{_DASH_URL}/api/friday/restart", timeout=5)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def quit_friday(self, _):
        """Quit the bar, and the core too when we are the one supervising it."""
        if self.supervisor is not None:
            self.supervisor.shutdown()
        rumps.quit_application()


if __name__ == "__main__":
    FridayMenuBar().run()
