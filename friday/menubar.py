"""
menubar.py
F.R.I.D.A.Y. menu bar app — standalone rumps app, never imported by friday.py.

Reads live status from the dashboard server's /api/status endpoint (which
distinguishes running / paused / offline). Falls back to pgrep if the server
is unreachable so the menubar still works during the brief startup window.

Launches the dashboard as a detached pywebview process. Provider switching
moved into the dashboard's AI Model page; menubar stays minimal.
"""

import os
import subprocess
import sys

import requests
import rumps

_HERE      = os.path.dirname(os.path.abspath(__file__))
_PYTHON    = sys.executable

_DASH_URL  = "http://127.0.0.1:5174"
_STATUS    = f"{_DASH_URL}/api/status"
_PAUSE     = f"{_DASH_URL}/api/friday/pause"
_BRIEF     = f"{_DASH_URL}/api/friday/brief"


def _friday_running_proc() -> bool:
    return subprocess.run(["pgrep", "-f", "friday.py"], capture_output=True).returncode == 0


def _status_snapshot() -> dict:
    """Return {'state': 'online'|'paused'|'offline'|'error'}."""
    try:
        r = requests.get(_STATUS, timeout=1.5)
        if r.status_code != 200:
            return {"state": "error" if _friday_running_proc() else "offline"}
        data = r.json()
        if data.get("status") == "running" and data.get("paused"):
            return {"state": "paused"}
        if data.get("status") == "running":
            return {"state": "online"}
        return {"state": "offline"}
    except Exception:
        return {"state": "error" if _friday_running_proc() else "offline"}


class FridayMenuBar(rumps.App):
    def __init__(self):
        # Title uses Unicode dots — we recolor by changing the glyph since
        # rumps title is a single-color string.
        super().__init__("F.R.I", quit_button=None)
        self._last_state = None

        self._brief     = rumps.MenuItem("Brief Me Now", callback=self.brief_me)
        self._pause     = rumps.MenuItem("Pause Friday", callback=self.toggle_pause)
        self._dashboard = rumps.MenuItem("Open Dashboard", callback=self.open_dashboard)
        self._quit      = rumps.MenuItem("Quit Friday Bar", callback=rumps.quit_application)

        self.menu = [
            self._brief,
            self._pause,
            None,
            self._dashboard,
            None,
            self._quit,
        ]

        self._tick(None)
        rumps.Timer(self._tick, 10).start()

    # ── Display ───────────────────────────────────────────────────────────

    def _tick(self, _):
        snap = _status_snapshot()
        st = snap["state"]
        # rumps title is single-color; convey state via glyph + word
        glyphs = {
            "online":  "● F.R.I",
            "paused":  "◐ F.R.I",
            "offline": "○ F.R.I",
            "error":   "✕ F.R.I",
        }
        self.title = glyphs.get(st, "F.R.I")
        if st != self._last_state:
            self._last_state = st
            self._pause.title = "Resume Friday" if st == "paused" else "Pause Friday"

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
        next_paused = (snap["state"] != "paused")
        try:
            r = requests.post(_PAUSE, json={"paused": next_paused}, timeout=5)
            if r.status_code != 200:
                rumps.alert("Friday", f"Pause failed: HTTP {r.status_code}")
            else:
                self._tick(None)
        except Exception as e:
            rumps.alert("Friday", f"Pause failed: {e}")

    def open_dashboard(self, _):
        # Open the local dashboard URL in the user's default browser.
        subprocess.Popen(["open", _DASH_URL], start_new_session=True)


if __name__ == "__main__":
    FridayMenuBar().run()
