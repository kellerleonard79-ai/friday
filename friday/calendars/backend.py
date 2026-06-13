"""
calendars/backend.py
Calendar backend dispatch — the one place that decides whether Friday's
event store is Apple Calendar (macOS, JXA) or Google Calendar (Windows, API).

Selection: config `calendar.backend` ("apple" | "google"), defaulting to
google on Windows and apple everywhere else.

friday.py calls init(config) once at startup so the write path (which is
reached from actions/calendar.py without a cfg in hand) knows the config.
Reads take cfg explicitly, matching the old apple_calendar signatures.
"""

import logging
import sys

logger = logging.getLogger("friday.calbackend")

_CONFIG: dict = {}


def init(config: dict) -> None:
    global _CONFIG
    _CONFIG = config or {}
    logger.info(f"Calendar backend: {backend_name(_CONFIG)}")


def backend_name(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else _CONFIG
    explicit = ((cfg.get("calendar") or {}).get("backend") or "").strip().lower()
    if explicit in ("apple", "google"):
        return explicit
    return "google" if sys.platform == "win32" else "apple"


def _mod(cfg: dict | None = None):
    if backend_name(cfg) == "google":
        from calendars import google_cal
        return google_cal
    from calendars import apple
    return apple


# ── Reads ─────────────────────────────────────────────────────────────────────

def events_in_window(cfg: dict, start_date, end_date) -> list[dict]:
    return _mod(cfg).events_in_window(cfg, start_date, end_date)


def events_for_day(cfg: dict, target_date) -> list[dict]:
    return _mod(cfg).events_for_day(cfg, target_date)


# ── Writes ────────────────────────────────────────────────────────────────────

def write_event(calendar_name: str, title: str, start, end,
                location: str = "", description: str = "",
                all_day: bool = False) -> str | None:
    return _mod().write_event(_CONFIG, calendar_name, title, start, end,
                              location=location, description=description,
                              all_day=all_day)


def calendar_exists(name: str) -> bool:
    return _mod().calendar_exists(_CONFIG, name)
