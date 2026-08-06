"""
compat.py
Small cross-platform shims so the same codebase runs on macOS and Windows.
"""

import sys
import tempfile
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS   = sys.platform == "darwin"

# What the Dock tile and the application menu call Friday's GUI processes.
APP_DISPLAY_NAME = "J.A.R.V.I.S."


def set_mac_app_name(name: str = APP_DISPLAY_NAME) -> None:
    """Name this process for the Dock and the application menu.

    A source checkout runs under Python.app, and LaunchServices reads the name
    off the main bundle — so the Dock tile reads "Python". The main bundle's
    info dictionary is mutable in memory: patching it renames only this
    process, and never touches Python.app on disk.

    Must be called before anything creates NSApplication (rumps' event loop,
    Tk's window, an icon swap). LaunchServices reads the name once, when the
    process registers, and ignores later edits.
    """
    if not IS_MACOS:
        return
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        info["CFBundleName"] = name
        info["CFBundleDisplayName"] = name
    except Exception:
        pass  # cosmetic only — never keep a GUI process from starting


def strftime(dt, fmt: str) -> str:
    """Portable strftime. glibc's no-pad flag ('%-d', '%-I') raises on
    Windows, where the equivalent flag is '%#d'. Call this instead of
    dt.strftime() for any format string containing '%-'."""
    if IS_WINDOWS:
        fmt = fmt.replace("%-", "%#")
    return dt.strftime(fmt)


def listening_flag_path() -> Path:
    """Transient flag file voice/listen.py touches during a PTT/wake session.
    /tmp on macOS; the system temp dir elsewhere."""
    if IS_MACOS:
        return Path("/tmp/friday_listening")
    return Path(tempfile.gettempdir()) / "friday_listening"
