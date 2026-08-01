"""
mac_app.py
macOS entry point for the packaged Friday.app shipped in the .dmg.

Mirrors tray.py's role on Windows:

    Friday.app            → this file. First launch runs the setup wizard,
                            then it supervises the core and shows the menu bar.
    Friday.app --core     → the agent itself (friday.main()).

A source checkout does not need this — it can keep running `menubar.py` and
`restart.sh`, or install LaunchAgents via macos_setup. This exists so the
drag-installed .app is self-contained: one process the user launches from
/Applications, no plists to edit, no Terminal.

Supervision lives here rather than in a KeepAlive LaunchAgent because the .app
must survive being launched from Finder by a user who has never opened a
terminal. "Start at Login" (macos_setup.install_login_item) adds the LaunchAgent
on top for the always-on case.
"""

import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import paths

API_BASE = "http://127.0.0.1:5174"
_SINGLETON_PORT = 51740  # held open to prevent a second instance

logger = logging.getLogger("friday.mac_app")


def _spawn_core() -> subprocess.Popen:
    """Launch the Friday core as a child process."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--core"]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--core"]
    return subprocess.Popen(cmd, close_fds=True, start_new_session=True)


def _run_wizard(first_run: bool) -> bool:
    """Run the setup wizard in a child process. Returns True if it saved.

    Never in-process: Tkinter creates and configures the process's
    NSApplication, and rumps would then inherit that instead of building its
    own — which loses the status item. A child process keeps this one clean.
    """
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--setup"]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--setup"]
    if first_run:
        cmd.append("--first-run")
    try:
        return subprocess.run(cmd, check=False).returncode == 0
    except Exception as e:
        logger.error(f"Setup wizard failed to launch: {e}")
        return False


def _api(method: str, path: str, json_body=None, timeout: float = 10):
    import requests
    url = f"{API_BASE}{path}"
    if method == "GET":
        return requests.get(url, timeout=timeout)
    return requests.post(url, json=json_body, timeout=timeout)


class CoreSupervisor:
    """Keeps friday.py alive. Identical policy to the Windows tray: a clean
    exit (what /api/friday/restart triggers) is a restart; rapid exits are a
    crash loop and get backed off so a bad config doesn't spin the CPU."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._quitting = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._supervise, daemon=True).start()

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
            uptime = time.monotonic() - started
            delay = 3 if uptime > 30 else 30
            logger.warning(f"Core exited after {uptime:.0f}s — restarting in {delay}s")
            if self._quitting.wait(delay):
                return

    def shutdown(self) -> None:
        self._quitting.set()
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


def _become_accessory() -> None:
    """Drop the Dock tile so Friday lives only in the menu bar.

    The bundle intentionally does not declare LSUIElement — an accessory
    process never gets Tk windows on screen, which would make the setup wizard
    invisible. Doing it here instead scopes it to the process that actually
    shows the menu bar, and has to happen before rumps starts its event loop.
    """
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)
    except Exception as e:
        logger.warning(f"Could not hide the Dock icon: {e}")


def _acquire_singleton() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _SINGLETON_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def _prime_calendar_access() -> None:
    """Ask for EventKit calendar access here, before the core is spawned.

    TCC grants are per executable binary, and the menu bar process and the core
    are the same binary — so a grant obtained here is one the core inherits.
    This has to happen in *this* process rather than in the core, because the
    core has no foreground UI: its request produces a prompt nobody is looking
    at, times out as "not determined", and demotes every calendar read for the
    rest of that process's life to the JXA fallback (minutes per briefing).

    Best-effort and non-blocking to the user's decision — a denied or ignored
    prompt just means the fallback, not a broken app.
    """
    try:
        from connectors import apple_calendar
        threading.Thread(target=apple_calendar.ensure_calendar_access,
                         daemon=True).start()
    except Exception as e:
        logger.debug(f"Calendar access priming skipped: {e}")


def main() -> None:
    if "--core" in sys.argv:
        import friday
        friday.main()
        return

    if "--setup" in sys.argv:
        # Both the first launch and the menu bar's "Run Setup Wizard" re-exec
        # the .app this way — Tkinter and rumps each insist on owning the main
        # thread of their process.
        import setup_wizard
        sys.exit(0 if setup_wizard.run(first_run="--first-run" in sys.argv) else 1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[logging.FileHandler(str(paths.log_dir() / "mac_app.log"))],
    )

    lock = _acquire_singleton()
    if lock is None:
        logger.info("Another Friday instance is running — bringing it forward.")
        subprocess.run(["open", API_BASE], check=False)
        return

    if not paths.config_path().exists() and not _run_wizard(first_run=True):
        logger.info("Setup cancelled — exiting.")
        return

    # The voice launcher reads its interpreter and script path from this file;
    # regenerating on every launch keeps it correct after the .app is moved.
    try:
        import macos_setup
        macos_setup.write_voice_launcher_conf()
    except Exception as e:
        logger.warning(f"Could not write the voice launcher conf: {e}")

    _prime_calendar_access()

    supervisor = CoreSupervisor()
    supervisor.start()

    _become_accessory()
    import menubar
    bar = menubar.FridayMenuBar()
    bar.supervisor = supervisor  # lets Quit take the core down with it
    try:
        bar.run()
    finally:
        supervisor.shutdown()


if __name__ == "__main__":
    main()
