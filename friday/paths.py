"""
paths.py
Single source of truth for where Friday's files live.

macOS (source checkout): everything stays inside the package directory,
exactly as before — config, memory/, logs/ next to friday.py.

Windows (and any frozen/PyInstaller build): the install dir (Program Files)
is read-only, so all mutable state — config, database, logs, OAuth tokens —
lives in %APPDATA%\\Friday. Read-only bundled resources (AGENTS.md,
quips.yaml, dashboard static files) resolve into the PyInstaller bundle.
"""

import os
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent

IS_FROZEN = bool(getattr(sys, "frozen", False))


def _use_appdata() -> bool:
    return sys.platform == "win32" or IS_FROZEN


def data_dir() -> Path:
    """Directory for all mutable state. Created on first call."""
    if _use_appdata():
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            d = Path(base) / "Friday"
        else:
            d = Path.home() / ".friday"
    else:
        d = _PKG_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def resource_path(*parts: str) -> Path:
    """Read-only bundled resource (AGENTS.md, quips.yaml, dashboard/static)."""
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", str(_PKG_DIR))).joinpath(*parts)
    return _PKG_DIR.joinpath(*parts)


def config_path() -> Path:
    return data_dir() / "friday_config.yaml"


def log_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tls_dir() -> Path:
    """Tailscale-issued HTTPS cert/key for the dashboard's tailnet bind.
    Not under resource_path() — these are fetched at runtime, not bundled."""
    d = data_dir() / "tls"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path(cfg: dict | None = None) -> Path:
    rel = ((cfg or {}).get("memory") or {}).get("db_path", "memory/friday_memory.db")
    p = Path(rel)
    if p.is_absolute():
        return p
    return data_dir() / p


def google_token_path() -> Path:
    return data_dir() / "google_token.json"


def google_client_secret_path() -> Path:
    """OAuth client for the Google Calendar backend. The wizard copies the
    file here; a bundled copy (shipped with the installer) is the fallback."""
    local = data_dir() / "google_client_secret.json"
    if local.exists():
        return local
    bundled = resource_path("google_client_secret.json")
    if bundled.exists():
        return bundled
    return local
