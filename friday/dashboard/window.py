"""
dashboard/window.py
Standalone pywebview process that renders the F.R.I.D.A.Y. dashboard.

Launched by menubar.py as a detached subprocess so the window lifecycle is
independent of both the menubar and the main agent process. Connects to the
FastAPI server already running inside friday.py at 127.0.0.1:5174.

Run directly:  python -m dashboard.window
"""

import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import webview

URL = "http://127.0.0.1:5174/"


def _wait_for_server(timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urlopen(URL, timeout=1).close()
            return True
        except (URLError, ConnectionError, OSError):
            time.sleep(0.3)
    return False


def main() -> None:
    if not _wait_for_server():
        print(
            "Friday dashboard server is not reachable at "
            f"{URL}. Is friday.py running?",
            file=sys.stderr,
        )
        sys.exit(1)

    webview.create_window(
        "F.R.I.D.A.Y.",
        URL,
        width=1200,
        height=800,
        min_size=(900, 600),
        frameless=True,
        easy_drag=True,
        background_color="#1A0800",
    )
    webview.start()


if __name__ == "__main__":
    main()
