"""
agent/briefings.py
Briefing prompt composers. Synchronous — wrap in run_in_executor when called from async.

Each function shapes the LLM call. Persona comes from agent._think()'s system_instruction.
Returns the model's prose, or "" on failure (caller decides whether to send).
"""

from datetime import date, datetime, timedelta


def _local(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def _fmt_evt(evt: dict) -> str:
    """One line per event: '9:00 AM — Title (Calendar)[, Location]'."""
    dt = _local(evt.get("start_iso", ""))
    when = dt.strftime("%-I:%M %p") if dt else "??:??"
    title = evt.get("title", "(untitled)")
    line = f"{when} — {title} ({evt.get('calendar', '?')})"
    if evt.get("location"):
        line += f", {evt['location']}"
    return line


def _fmt_upcoming(evt: dict, today: date) -> str:
    """'Mon Jun 1 — Title' with day-delta tag for items in 1/3/5-day windows."""
    dt = _local(evt.get("start_iso", ""))
    day_label = dt.strftime("%a %b %-d") if dt else "??"
    title = evt.get("title", "(untitled)")
    days_out = (dt.date() - today).days if dt else None
    tag = ""
    if days_out is not None:
        if days_out <= 1:
            tag = " [DUE IN 1 DAY]"
        elif days_out <= 3:
            tag = " [DUE IN 3 DAYS]"
        elif days_out <= 5:
            tag = " [DUE IN 5 DAYS]"
    return f"{day_label} — {title}{tag}"


def _fmt_canvas(row: tuple) -> str:
    """SQLite row: (title, due_at, urgency)."""
    title, due_at, urgency = row
    when = ""
    if due_at:
        dt = _local(due_at)
        when = f" (due {dt.strftime('%a %b %-d, %-I:%M %p')})" if dt else f" (due {due_at})"
    return f"[{urgency}] {title}{when}"


def _events_block(evts: list[dict]) -> str:
    if not evts:
        return "  (none)"
    return "\n".join("  " + _fmt_evt(e) for e in evts)


def _upcoming_block(evts: list[dict], today: date) -> str:
    if not evts:
        return "  (none)"
    return "\n".join("  " + _fmt_upcoming(e, today) for e in evts)


def _canvas_block(rows: list[tuple]) -> str:
    if not rows:
        return "  (none)"
    return "\n".join("  " + _fmt_canvas(r) for r in rows)


# ── Composers ─────────────────────────────────────────────────────────────────


def compose_morning(agent, today_evts: list[dict], upcoming_evts: list[dict],
                    weather_str: str) -> str:
    today = date.today()
    today_label = today.strftime("%A, %B %-d")
    weather_line = f"Weather: {weather_str}" if weather_str else "Weather: (unavailable)"

    prompt = (
        f"Compose the morning briefing for {today_label}.\n\n"
        f"{weather_line}\n\n"
        f"Today's scheduled events:\n{_events_block(today_evts)}\n\n"
        f"Upcoming this week (Apple Calendar):\n{_upcoming_block(upcoming_evts, today)}\n\n"
        f"Start with exactly: \"Good morning, sir. Here is your day:\"\n"
        f"Then in plain prose (no bullets, no markdown), 3-5 sentences:\n"
        f"  - Walk through today's events chronologically.\n"
        f"  - Mention anything tagged DUE IN 1/3/5 DAYS with appropriate emphasis.\n"
        f"  - One short weather note if notable.\n"
        f"  - If today has no events, say so plainly and pivot to the week ahead."
    )
    return agent._think(prompt, use_tools=False)


def compose_evening(agent, tomorrow_evts: list[dict], upcoming_evts: list[dict],
                    canvas_pending: list[tuple], weather_str: str) -> str:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    tomorrow_label = tomorrow.strftime("%A, %B %-d")
    weather_line = f"Current weather: {weather_str}" if weather_str else ""

    prompt = (
        f"Compose the evening briefing. Looking ahead to {tomorrow_label}.\n\n"
        f"{weather_line}\n\n"
        f"Tomorrow's scheduled events:\n{_events_block(tomorrow_evts)}\n\n"
        f"Upcoming this week:\n{_upcoming_block(upcoming_evts, today)}\n\n"
        f"Pending Canvas items (SOON or URGENT, not yet alerted):\n"
        f"{_canvas_block(canvas_pending)}\n\n"
        f"Write in plain prose, no markdown, 3-5 sentences:\n"
        f"  - Preview tomorrow chronologically.\n"
        f"  - Surface anything tagged DUE IN 1/3/5 DAYS and pending Canvas URGENT/SOON items.\n"
        f"  - End with a brief, professional sign-off (e.g. \"Rest well, sir.\")."
    )
    return agent._think(prompt, use_tools=False)


def compose_on_demand(agent, today_evts: list[dict], tomorrow_evts: list[dict],
                      upcoming_evts: list[dict], canvas_pending: list[tuple],
                      weather_str: str) -> str:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    weather_line = f"Weather: {weather_str}" if weather_str else ""

    prompt = (
        f"The user asked for a briefing. Give him a concise current snapshot.\n\n"
        f"{weather_line}\n\n"
        f"Today ({today.strftime('%A, %B %-d')}):\n{_events_block(today_evts)}\n\n"
        f"Tomorrow ({tomorrow.strftime('%A, %B %-d')}):\n{_events_block(tomorrow_evts)}\n\n"
        f"Upcoming this week:\n{_upcoming_block(upcoming_evts, today)}\n\n"
        f"Pending Canvas items (SOON or URGENT):\n{_canvas_block(canvas_pending)}\n\n"
        f"Plain prose, no markdown, 3-5 sentences. Cover today, tomorrow, anything "
        f"tagged DUE IN 1/3/5 DAYS in the upcoming list, and any pending Canvas urgency. "
        f"If everything is empty, say so plainly."
    )
    return agent._think(prompt, use_tools=False)
