"""
calendars/apple.py
Apple Calendar backend — reads and writes via JXA (osascript -l JavaScript).
macOS only. Reads are delegated to connectors/apple_calendar.py (unchanged);
the low-level write/exists JXA lives here so calendars/backend.py is the one
write path for every platform.

Synchronous — always wrap calls in run_in_executor inside async handlers.
"""

import json
import logging
import subprocess
from datetime import date, datetime

from connectors.apple_calendar import events_for_day, events_in_window  # noqa: F401

logger = logging.getLogger("friday.applecal.write")
_TIMEOUT_S = 15


def write_event(cfg: dict, calendar_name: str, title: str, start: datetime,
                end: datetime, location: str = "", description: str = "",
                all_day: bool = False) -> str | None:
    """Create an event in the named Apple Calendar. Returns Apple UID or None
    on failure. No dedup — caller's responsibility."""
    payload = {
        "calendar":    calendar_name,
        "summary":     title,
        "location":    location,
        "description": description,
        "allDay":      bool(all_day),
    }
    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(end.timestamp() * 1000)
    script = f"""
const Calendar = Application('Calendar');
const data = {json.dumps(payload)};
const targets = Calendar.calendars.whose({{name: data.calendar}})();
if (targets.length === 0) {{
  JSON.stringify({{error: "calendar not found: " + data.calendar}});
}} else {{
  try {{
    const e = Calendar.Event({{
      summary:     data.summary,
      startDate:   new Date({start_ms}),
      endDate:     new Date({end_ms}),
      location:    data.location,
      description: data.description,
      alldayEvent: data.allDay
    }});
    targets[0].events.push(e);
    JSON.stringify({{uid: e.uid()}});
  }} catch (err) {{
    JSON.stringify({{error: err.toString()}});
  }}
}}
"""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning(f"Apple Calendar write timed out for {title!r}")
        return None
    if result.returncode != 0:
        logger.error(f"Apple Calendar write failed: {result.stderr.strip()[:200]}")
        return None
    try:
        out = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        logger.error(f"Apple Calendar write — bad JSON: {result.stdout[:200]}")
        return None
    if "error" in out:
        logger.error(f"Apple Calendar write error: {out['error']}")
        return None
    return out.get("uid")


def _run_jxa(script: str) -> dict | None:
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return None


def calendar_exists(cfg: dict, name: str) -> bool:
    out = _run_jxa(f"""
const Calendar = Application('Calendar');
const t = Calendar.calendars.whose({{name: {json.dumps(name)}}})();
JSON.stringify({{exists: t.length > 0}});
""")
    return bool((out or {}).get("exists"))
