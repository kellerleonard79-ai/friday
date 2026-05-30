"""
connectors/apple_calendar.py
Read-only Apple Calendar reader via JXA (osascript -l JavaScript).
Returns events for a date or window, filtered by a config whitelist of calendar names.

Synchronous — always wrap calls in run_in_executor inside async handlers.
"""

import json
import logging
import subprocess
from datetime import date, datetime, timedelta

logger = logging.getLogger("friday.applecal")

_DEFAULT_EXCLUDED = ("Siri Suggestions", "US Holidays")
_TIMEOUT_S = 15


def _whitelist(cfg: dict) -> list[str] | None:
    cal_list = (cfg.get("agent", {}) or {}).get("briefing_calendars") or []
    return list(cal_list) if cal_list else None


def _jxa_script(whitelist: list[str] | None, start: datetime, end: datetime) -> str:
    whitelist_js = json.dumps(whitelist) if whitelist is not None else "null"
    excluded_js  = json.dumps(list(_DEFAULT_EXCLUDED))
    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(end.timestamp() * 1000)
    return f"""
const Calendar = Application('Calendar');
const startDate = new Date({start_ms});
const endDate   = new Date({end_ms});
const whitelist = {whitelist_js};
const excluded  = new Set({excluded_js});
const out = [];
const cals = Calendar.calendars();
for (let i = 0; i < cals.length; i++) {{
    const cal = cals[i];
    let name;
    try {{ name = cal.name(); }} catch (e) {{ continue; }}
    if (whitelist) {{
        if (whitelist.indexOf(name) === -1) continue;
    }} else if (excluded.has(name)) {{
        continue;
    }}
    try {{
        const evts = cal.events.whose({{_and: [
            {{startDate: {{">=": startDate}}}},
            {{startDate: {{"<": endDate}}}}
        ]}})();
        for (let j = 0; j < evts.length; j++) {{
            const e = evts[j];
            try {{
                out.push({{
                    title:     (e.summary()  || "").toString(),
                    start_iso: e.startDate().toISOString(),
                    end_iso:   e.endDate().toISOString(),
                    location:  (e.location() || "").toString(),
                    calendar:  name,
                    uid:       (e.uid()      || "").toString()
                }});
            }} catch (err) {{}}
        }}
    }} catch (err) {{}}
}}
JSON.stringify(out);
"""


def _run_jxa(script: str) -> list[dict]:
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Apple Calendar read timed out (>15s)")
        return []

    if result.returncode != 0:
        logger.error(f"osascript failed: {result.stderr.strip()[:200]}")
        return []

    out = result.stdout.strip()
    if not out:
        return []

    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        logger.error(f"Apple Calendar JSON parse error: {e}")
        return []


def events_in_window(cfg: dict, start_date: date, end_date: date) -> list[dict]:
    """All events with start in [start_date 00:00 local, end_date 00:00 local)."""
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt   = datetime.combine(end_date,   datetime.min.time())
    script   = _jxa_script(_whitelist(cfg), start_dt, end_dt)
    events   = _run_jxa(script)
    events.sort(key=lambda e: e.get("start_iso", ""))
    return events


def events_for_day(cfg: dict, target_date: date) -> list[dict]:
    """All events starting on target_date in whitelisted calendars."""
    return events_in_window(cfg, target_date, target_date + timedelta(days=1))
