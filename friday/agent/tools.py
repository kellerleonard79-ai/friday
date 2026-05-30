"""
agent/tools.py
Synchronous tool functions Gemini can invoke. The google-genai SDK auto-handles
the call loop when these are passed as `tools=[...]` to generate_content.

Each tool:
  - Has a clear docstring (Gemini's tool description)
  - Has typed parameters (Gemini's schema inference uses annotations)
  - Returns a JSON-serializable dict — never raises
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from connectors import apple_calendar, weather

logger = logging.getLogger("friday.tools")


def make_tools(conn, config):
    """Return the list of tool callables bound to this Friday instance."""

    tz_name = (config.get("agent") or {}).get("timezone", "America/Chicago")
    local_tz = ZoneInfo(tz_name)

    def _to_local(iso_utc: str) -> str:
        """Apple Calendar's JXA emits UTC ISO strings ('...Z'). Convert to the
        user's local timezone so the LLM doesn't misread them as local."""
        if not iso_utc:
            return ""
        try:
            dt_utc = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
            return dt_utc.astimezone(local_tz).strftime("%Y-%m-%d %H:%M %Z")
        except ValueError:
            return iso_utc

    def get_now() -> dict:
        """Return the current local date, time, and day of week. Call this first
        whenever the user mentions relative times like 'today', 'tomorrow',
        'next week', 'in 3 days', 'this weekend'."""
        now = datetime.now(local_tz)
        return {
            "iso":         now.isoformat(timespec="seconds"),
            "date":        now.date().isoformat(),
            "day_of_week": now.strftime("%A"),
            "timezone":    tz_name,
            "human":       now.strftime("%A, %B %-d %Y, %-I:%M %p %Z"),
        }

    def get_schedule(start_date: str, end_date: str) -> dict:
        """Return events from the user's Apple Calendar with start times in
        [start_date, end_date). Both dates must be ISO YYYY-MM-DD; end_date is
        exclusive. Only whitelisted calendars are included. Use this for any
        schedule, calendar, appointment, or 'what am I doing' question.
        For a single day pass end_date = start_date + 1.

        Times in returned events are already in the user's local timezone —
        present them to the user as-is. Do not apply additional offsets."""
        try:
            s = date.fromisoformat(start_date)
            e = date.fromisoformat(end_date)
        except ValueError as err:
            return {"error": f"Invalid date format: {err}", "events": []}
        raw = apple_calendar.events_in_window(config, s, e)
        events = [{
            "title":    ev.get("title", ""),
            "start":    _to_local(ev.get("start_iso", "")),
            "end":      _to_local(ev.get("end_iso", "")),
            "location": ev.get("location", ""),
            "calendar": ev.get("calendar", ""),
        } for ev in raw]
        return {"timezone": tz_name, "count": len(events), "events": events}

    def get_weather(query: str = "") -> dict:
        """Return a natural-language weather string for the user's configured
        location. Pass the user's exact phrasing as `query` so the connector
        can pick up intent (rain vs. temperature vs. general forecast,
        time-of-day phrases like 'tonight' or 'at 3pm'). Use for any weather,
        rain, temperature, or forecast question."""
        text = weather.respond(config.get("weather", {}), query)
        return {"weather": text or "(weather unavailable)"}

    def get_pending_canvas() -> dict:
        """Return Canvas LMS assignments tagged URGENT or SOON that have not
        yet been alerted to the user. Use when the user asks what's due,
        what's pending, what's coming up in Canvas, or what assignments
        need attention."""
        if conn is None:
            return {"count": 0, "items": [], "error": "database unavailable"}
        rows = conn.execute(
            "SELECT title, due_at, urgency FROM events "
            "WHERE source='canvas' AND urgency IN ('URGENT','SOON') AND notified=0 "
            "ORDER BY due_at"
        ).fetchall()
        items = [{"title": r[0], "due_at": r[1], "urgency": r[2]} for r in rows]
        return {"count": len(items), "items": items}

    return [get_now, get_schedule, get_weather, get_pending_canvas]
