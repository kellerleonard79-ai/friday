"""
tray.py
Windows entry point — system tray app that supervises the Friday core.

Replaces the macOS menubar (rumps) + LaunchAgent pair:

    Friday.exe            → this tray app. First run launches the setup
                            wizard, then it spawns and supervises the core.
    Friday.exe --core     → the actual agent (friday.main()).

The supervisor relaunches the core whenever it exits, so the dashboard's
/api/friday/restart endpoint (which makes the core exit cleanly on Windows)
becomes a restart. Quit from the tray sets a flag so the exit is final.

macOS keeps menubar.py — this file is never used there.
"""

import logging
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import paths

API_BASE = "http://127.0.0.1:5174"
_SINGLETON_PORT = 51740  # held open to prevent a second tray instance

logger = logging.getLogger("friday.tray")


def _spawn_core() -> subprocess.Popen:
    """Launch the Friday core as a hidden child process."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--core"]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--core"]
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(cmd, creationflags=creationflags, close_fds=True)


def _api(method: str, path: str, json_body=None, timeout: float = 10):
    import requests
    url = f"{API_BASE}{path}"
    if method == "GET":
        return requests.get(url, timeout=timeout)
    return requests.post(url, json=json_body, timeout=timeout)


def _make_icon_image(color: str = "#ff8c1a"):
    """Round 'F' badge — drawn at runtime so no asset file is needed."""
    from PIL import Image, ImageDraw, ImageFont
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, size - 2, size - 2], fill=color)
    try:
        font = ImageFont.truetype("arialbd.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    d.text((size / 2, size / 2 - 2), "F", fill="white", font=font, anchor="mm")
    return img


class FridayTray:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._quitting = threading.Event()
        self._status: dict = {}
        self._icon = None

    # ── Supervision ──────────────────────────────────────────────────────────

    def _supervise(self) -> None:
        while not self._quitting.is_set():
            started = time.monotonic()
            try:
                self._proc = _spawn_core()
                logger.info(f"Core started (pid {self._proc.pid})")
                self._proc.wait()
            except Exception as e:
                logger.error(f"Core spawn failed: {e}")
            if self._quitting.is_set():
                return
            # Quick deaths mean a crash loop (bad config, bad token) —
            # back off so we don't spin.
            uptime = time.monotonic() - started
            delay = 3 if uptime > 30 else 30
            logger.warning(f"Core exited after {uptime:.0f}s — restarting in {delay}s")
            if self._quitting.wait(delay):
                return

    def _poll_status(self) -> None:
        while not self._quitting.wait(10):
            try:
                r = _api("GET", "/api/status", timeout=3)
                self._status = r.json() if r.status_code == 200 else {}
            except Exception:
                self._status = {}
            if self._icon is not None:
                self._icon.update_menu()

    # ── Menu actions ─────────────────────────────────────────────────────────

    def _status_text(self, _item=None) -> str:
        if not self._status:
            return "Friday — starting…"
        if self._status.get("paused"):
            return "Friday — paused"
        return f"Friday — online ({self._status.get('model') or '?'})"

    def _pause_text(self, _item=None) -> str:
        return "Resume Friday" if self._status.get("paused") else "Pause Friday"

    def _on_brief(self, icon, item) -> None:
        try:
            _api("POST", "/api/friday/brief")
        except Exception as e:
            logger.error(f"Brief failed: {e}")

    def _on_pause(self, icon, item) -> None:
        try:
            _api("POST", "/api/friday/pause",
                 {"paused": not self._status.get("paused")})
        except Exception as e:
            logger.error(f"Pause toggle failed: {e}")

    def _on_dashboard(self, icon, item) -> None:
        webbrowser.open(API_BASE)

    def _on_restart(self, icon, item) -> None:
        self._shutdown_core()

    def _on_wizard(self, icon, item) -> None:
        # Re-run setup in-place, then bounce the core to pick up the config.
        try:
            import setup_wizard
            if setup_wizard.run(first_run=False):
                self._shutdown_core()
        except Exception as e:
            logger.error(f"Setup wizard failed: {e}")

    def _on_quit(self, icon, item) -> None:
        self._quitting.set()
        self._shutdown_core()
        icon.stop()

    def _shutdown_core(self) -> None:
        """Ask the core to exit cleanly via its own API; fall back to a hard
        terminate. The supervisor decides whether it comes back."""
        proc = self._proc
        try:
            _api("POST", "/api/friday/restart", timeout=5)
        except Exception:
            pass
        if proc is None:
            return
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            logger.warning("Core didn't exit cleanly — terminating")
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    # ── Run ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        import pystray

        threading.Thread(target=self._supervise, daemon=True).start()
        threading.Thread(target=self._poll_status, daemon=True).start()

        menu = pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Brief Me Now", self._on_brief),
            pystray.MenuItem(self._pause_text, self._on_pause),
            pystray.MenuItem("Open Dashboard", self._on_dashboard),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart Friday", self._on_restart),
            pystray.MenuItem("Run Setup Wizard", self._on_wizard),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit Friday", self._on_quit),
        )
        self._icon = pystray.Icon("Friday", _make_icon_image(), "Friday", menu)
        self._icon.run()


def _acquire_singleton() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLETON_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def main() -> None:
    if "--core" in sys.argv:
        import friday
        friday.main()
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[logging.FileHandler(str(paths.log_dir() / "tray.log"))],
    )

    lock = _acquire_singleton()
    if lock is None:
        logger.info("Another Friday tray instance is running — exiting.")
        return

    if not paths.config_path().exists():
        import setup_wizard
        if not setup_wizard.run(first_run=True):
            logger.info("Setup cancelled — exiting.")
            return

    FridayTray().run()


if __name__ == "__main__":
    main()
