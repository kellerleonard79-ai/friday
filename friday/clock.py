"""
clock.py
Friday's notion of "now".

One rule, and it is the reason this module exists rather than a call to
datetime.now() at each site: **the configured timezone wins over the host
clock.** The always-on Mac's system timezone can disagree with
agent.timezone — after a DST change, after travel, after a bad NTP sync — and
when it does, every date-keyed calendar fetch (which uses the config tz) stays
correct while the weekday label drifts by a day. That off-by-one is what this
guards, and it must survive any refactor of this file.

Lives at the top level, not under agent/ or llm/, because both the briefing
composer and the dispatcher need it and neither may import the other.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import compat

DEFAULT_TIMEZONE = "America/Chicago"


def timezone_name(config: dict) -> str:
    return (config.get("agent") or {}).get("timezone", DEFAULT_TIMEZONE)


def local_now(config: dict) -> datetime:
    """Authoritative current datetime in the configured timezone.

    Never use date.today() or datetime.now() for Friday's notion of "today" —
    those read the host's system timezone. Derive it from here.
    """
    return datetime.now(ZoneInfo(timezone_name(config)))


def human(dt: datetime) -> str:
    """"Monday, August 11, 2026, 11:42 AM CDT" — the form written for a person.

    compat.strftime, not strftime: %-d and %-I are glibc-only and crash on the
    Windows build.
    """
    return compat.strftime(dt, "%A, %B %-d, %Y, %-I:%M %p %Z")
