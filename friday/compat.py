"""
compat.py
Small cross-platform shims so the same codebase runs on macOS and Windows.
"""

import sys
import tempfile
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MACOS   = sys.platform == "darwin"


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
