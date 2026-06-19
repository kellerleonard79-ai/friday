"""
agent/briefings.py
Briefing prompt composers + deterministic context bundling.

Each composer shapes one LLM call. Persona comes from agent._think()'s
system_instruction. Returns the model's prose, or "" on failure (caller decides
whether to send).

Context bundling (bundle_briefing_context / format_briefing_context):
    A briefing needs a known-complete dataset. Rather than leave the model to
    decide whether to call tools, we pre-fetch everything a briefing needs
    (calendar, Canvas, weather, high-priority GroupMe) by reusing the same
    underlying functions the LLM tools wrap, and inject it as a structured
    block at the top of the prompt. The briefing call runs with tools OFF
    (use_tools=False) — a hard guarantee of zero tool calls. The model has no
    discretion over what data to pull, so a weak briefing is always a bundle
    problem, never a "did the model drill into a tool" rabbit hole. If a
    briefing turns up thin, expand the bundler — don't re-enable tools.

    This applies to briefings ONLY — the chat handler (agent/core.py) and the
    GroupMe event-extraction path are deliberately untouched.

Synchronous — wrap in run_in_executor when called from async.
"""

import logging
from datetime import date, datetime, timedelta

import compat
from calendars import backend as calendar_backend
from connectors import weather as weather_connector

logger = logging.getLogger("friday.briefings")

# Sentinel stored in a bundle field when its fetch failed. The formatter renders
# it verbatim so a thin briefing is visible in both the message and the logs.
_UNAVAILABLE = "unavailable"

# How far back to look for high-priority GroupMe chatter worth surfacing.
_GROUPME_WINDOW_HOURS = 12


def _local(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def _is_all_day(evt: dict) -> bool:
    """Neither calendar backend returns an explicit all-day flag, so derive it.
    Google all-day events carry a date-only 'date' value (no 'T'); Apple all-day
    events come through as a midnight→midnight datetime spanning whole days."""
    start = evt.get("start_iso", "")
    if start and "T" not in start and len(start) == 10:
        return True
    sd = _local(start)
    ed = _local(evt.get("end_iso", ""))
    if sd and ed and sd.hour == 0 and sd.minute == 0:
        span = (ed - sd).total_seconds()
        if span >= 86400 and span % 86400 == 0:
            return True
    return False


def _fmt_evt(evt: dict) -> str:
    """One line per event: '9:00 AM — Title (Calendar)[, Location]'."""
    dt = _local(evt.get("start_iso", ""))
    when = compat.strftime(dt, "%-I:%M %p") if dt else "??:??"
    title = evt.get("title", "(untitled)")
    line = f"{when} — {title} ({evt.get('calendar', '?')})"
    if evt.get("location"):
        line += f", {evt['location']}"
    return line


def _fmt_upcoming(evt: dict, today: date) -> str:
    """'Mon Jun 1 — Title'. The LLM phrases proximity naturally from the date."""
    dt = _local(evt.get("start_iso", ""))
    day_label = compat.strftime(dt, "%a %b %-d") if dt else "??"
    title = evt.get("title", "(untitled)")
    return f"{day_label} — {title}"


def _fmt_canvas(row: tuple) -> str:
    """SQLite row: (title, due_at, urgency)."""
    title, due_at, urgency = row
    when = ""
    if due_at:
        dt = _local(due_at)
        when = f" (due {compat.strftime(dt, '%a %b %-d, %-I:%M %p')})" if dt else f" (due {due_at})"
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


# ── Deterministic context bundling ────────────────────────────────────────────


def _safe_list(value) -> list:
    """Bundle fields hold either real data or the _UNAVAILABLE sentinel string.
    The existing prompt-body builders expect lists; coerce the sentinel away."""
    return value if isinstance(value, list) else []


def _parse_groupme_row(body: str, created_at: str) -> dict:
    """Pull (time, group, sender, preview) out of a stored GroupMe events row.
    The groupme connector writes a fixed body layout (see connectors/groupme.py):
        [priority=high]
        Group: <name>
        From: <sender>
        <blank>
        <text...>
    """
    group = sender = ""
    text_lines: list[str] = []
    in_text = False
    for line in body.splitlines():
        if in_text:
            text_lines.append(line)
            continue
        if line.startswith("Group: "):
            group = line[len("Group: "):].strip()
        elif line.startswith("From: "):
            sender = line[len("From: "):].strip()
        elif line.strip() == "" and sender:
            in_text = True  # blank line after the header → body text follows
    preview = " ".join(" ".join(text_lines).split())[:120]
    dt = _local(created_at)
    when = compat.strftime(dt, "%-I:%M %p") if dt else "??:??"
    return {"time": when, "group": group or "?", "sender": sender or "?", "preview": preview}


def _fetch_calendar_window(config, start, end, label):
    """events_in_window with its own guard so one bad read can't sink the bundle."""
    try:
        return calendar_backend.events_in_window(config, start, end)
    except Exception as e:
        logger.warning(f"Briefing bundle: {label} fetch failed — {e}")
        return _UNAVAILABLE


def _fetch_calendar_day(config, day, label):
    try:
        return calendar_backend.events_for_day(config, day)
    except Exception as e:
        logger.warning(f"Briefing bundle: {label} fetch failed — {e}")
        return _UNAVAILABLE


def _fetch_canvas_pending(conn):
    try:
        return conn.execute(
            "SELECT title, due_at, urgency FROM events "
            "WHERE source='canvas' AND urgency IN ('URGENT','SOON') AND notified=0 "
            "ORDER BY due_at"
        ).fetchall()
    except Exception as e:
        logger.warning(f"Briefing bundle: canvas_pending fetch failed — {e}")
        return _UNAVAILABLE


def _fetch_weather(config, query, label):
    try:
        wx = weather_connector.respond(config.get("weather", {}), query)
        return wx or _UNAVAILABLE
    except Exception as e:
        logger.warning(f"Briefing bundle: {label} fetch failed — {e}")
        return _UNAVAILABLE


def _fetch_groupme_high_priority(conn):
    """High-priority GroupMe rows from the events buffer in the last 12h. Reads
    the table the connector already populated — no live API call here."""
    try:
        cutoff = (datetime.now().astimezone() - timedelta(hours=_GROUPME_WINDOW_HOURS)).isoformat()
        rows = conn.execute(
            "SELECT body, created_at FROM events "
            "WHERE source='groupme' AND body LIKE '%[priority=high]%' AND created_at >= ? "
            "ORDER BY created_at",
            (cutoff,),
        ).fetchall()
        return [_parse_groupme_row(body, created_at) for body, created_at in rows]
    except Exception as e:
        logger.warning(f"Briefing bundle: groupme_high_priority fetch failed — {e}")
        return _UNAVAILABLE


def bundle_briefing_context(slot: str, config: dict, conn) -> dict:
    """Pre-fetch everything the briefing needs, each source guarded individually.
    Always returns a dict — a single failed fetch yields the _UNAVAILABLE
    sentinel for that field, never an aborted briefing.

    Morning and evening differ only by their date windows:
      morning → today + next 3 days (excluding today), weather today
      evening → tomorrow + next 5 days (from tomorrow), weather tomorrow
    """
    today = date.today()
    tomorrow = today + timedelta(days=1)
    bundle: dict = {"slot": slot, "now": datetime.now().astimezone()}

    if slot == "morning":
        bundle["today_calendar"] = _fetch_calendar_day(config, today, "today_calendar")
        # next 3 days excluding today
        bundle["week_preview"] = _fetch_calendar_window(
            config, tomorrow, today + timedelta(days=4), "week_preview"
        )
        bundle["weather_today"] = _fetch_weather(config, "weather today", "weather_today")
    else:  # evening
        bundle["tomorrow_calendar"] = _fetch_calendar_day(config, tomorrow, "tomorrow_calendar")
        # next 5 days starting tomorrow
        bundle["week_ahead"] = _fetch_calendar_window(
            config, tomorrow, tomorrow + timedelta(days=5), "week_ahead"
        )
        bundle["weather_tomorrow"] = _fetch_weather(config, "weather tomorrow", "weather_tomorrow")

    bundle["canvas_pending"] = _fetch_canvas_pending(conn)
    bundle["groupme_high_priority"] = _fetch_groupme_high_priority(conn)

    _log_bundle_summary(bundle)
    return bundle


def _count(value) -> int:
    return len(value) if isinstance(value, list) else 0


def _log_bundle_summary(bundle: dict) -> None:
    """One INFO line so future 'weak briefing' complaints can be diagnosed by
    checking whether the bundle was thin or full. Full block at DEBUG."""
    slot = bundle.get("slot", "?")
    cal = bundle.get("today_calendar" if slot == "morning" else "tomorrow_calendar")
    weather = bundle.get("weather_today" if slot == "morning" else "weather_tomorrow")
    weather_state = "unavailable" if weather == _UNAVAILABLE or not weather else "OK"
    logger.info(
        f"Briefing context bundled: {slot}, {_count(cal)} cal events, "
        f"{_count(bundle.get('canvas_pending'))} canvas items, weather {weather_state}, "
        f"{_count(bundle.get('groupme_high_priority'))} groupme high-priority"
    )
    logger.debug("Briefing context block:\n%s", format_briefing_context(bundle))


# ── Context block formatting (the injected, human-scannable section) ──────────


def _block_day_events(value) -> str:
    """Lines for a single day's calendar (today/tomorrow)."""
    if value == _UNAVAILABLE:
        return "  unavailable"
    evts = _safe_list(value)
    if not evts:
        return "  No events scheduled."
    lines = []
    for e in evts:
        title = e.get("title", "(untitled)")
        if _is_all_day(e):
            lines.append(f"  - {title} (all day)")
            continue
        sdt = _local(e.get("start_iso", ""))
        edt = _local(e.get("end_iso", ""))
        start = compat.strftime(sdt, "%H:%M") if sdt else "??:??"
        if edt and sdt and edt.date() == sdt.date():
            lines.append(f"  - {start}–{compat.strftime(edt, '%H:%M')} {title}")
        else:
            lines.append(f"  - {start} {title}")
    return "\n".join(lines)


def _block_week(value) -> str:
    """Lines for a multi-day preview/look-ahead window."""
    if value == _UNAVAILABLE:
        return "  unavailable"
    evts = _safe_list(value)
    if not evts:
        return "  Nothing scheduled."
    lines = []
    for e in evts:
        title = e.get("title", "(untitled)")
        sdt = _local(e.get("start_iso", ""))
        day = compat.strftime(sdt, "%a %b %-d") if sdt else "??"
        if _is_all_day(e):
            lines.append(f"  - {day}: {title} (all day)")
        else:
            t = compat.strftime(sdt, "%H:%M") if sdt else "??:??"
            lines.append(f"  - {day} {t}: {title}")
    return "\n".join(lines)


def _block_canvas(value) -> str:
    if value == _UNAVAILABLE:
        return "  unavailable"
    rows = _safe_list(value)
    if not rows:
        return "  No pending Canvas items."
    lines = []
    for title, due_at, urgency in rows:
        when = ""
        if due_at:
            dt = _local(due_at)
            when = f" — due {compat.strftime(dt, '%a %b %-d, %-I:%M %p')}" if dt else f" — due {due_at}"
        lines.append(f"  - ({urgency}) {title}{when}")
    return "\n".join(lines)


def _block_weather(value) -> str:
    if value == _UNAVAILABLE or not value:
        return "  unavailable"
    return f"  - {value}"


def _block_groupme(value) -> str:
    if value == _UNAVAILABLE:
        return "  unavailable"
    items = _safe_list(value)
    if not items:
        return "  Nothing high-priority."
    lines = []
    for it in items:
        lines.append(
            f"  - {it['time']}, \"{it['group']}\", {it['sender']}: \"{it['preview']}\""
        )
    return "\n".join(lines)


def format_briefing_context(bundle: dict) -> str:
    """Render the bundle as a delimited, scannable block for the prompt top."""
    slot = bundle.get("slot", "?")
    now = bundle.get("now")
    now_str = compat.strftime(now, "%A, %B %-d %Y, %H:%M %Z") if isinstance(now, datetime) else "?"

    parts = [
        "===== BRIEFING CONTEXT (deterministic, do not re-fetch) =====",
        f"Now: {now_str}",
        "",
    ]
    if slot == "morning":
        parts += [
            "Today's calendar:",
            _block_day_events(bundle.get("today_calendar")),
            "",
            "Week ahead (next 3 days):",
            _block_week(bundle.get("week_preview")),
            "",
            "Canvas pending:",
            _block_canvas(bundle.get("canvas_pending")),
            "",
            "Weather today:",
            _block_weather(bundle.get("weather_today")),
            "",
        ]
    else:
        parts += [
            "Tomorrow's calendar:",
            _block_day_events(bundle.get("tomorrow_calendar")),
            "",
            "Week ahead (next 5 days):",
            _block_week(bundle.get("week_ahead")),
            "",
            "Canvas pending:",
            _block_canvas(bundle.get("canvas_pending")),
            "",
            "Weather tomorrow:",
            _block_weather(bundle.get("weather_tomorrow")),
            "",
        ]
    parts += [
        f"GroupMe (high priority, last {_GROUPME_WINDOW_HOURS}h):",
        _block_groupme(bundle.get("groupme_high_priority")),
        "===== END CONTEXT =====",
    ]
    return "\n".join(parts)


# ── Composers ─────────────────────────────────────────────────────────────────


def compose_morning(agent, bundle: dict) -> str:
    """Morning briefing from the pre-bundled context. The deterministic block is
    injected above the existing instructions; tools stay registered as a
    fallback but the context is complete, so a normal day needs zero tool calls.
    """
    today = date.today()
    today_label = compat.strftime(today, "%A, %B %-d")
    today_evts = _safe_list(bundle.get("today_calendar"))
    upcoming_evts = _safe_list(bundle.get("week_preview"))
    weather_str = bundle.get("weather_today")
    weather_line = (
        f"Weather: {weather_str}" if weather_str and weather_str != _UNAVAILABLE
        else "Weather: (unavailable)"
    )

    context_block = format_briefing_context(bundle)
    prompt = (
        f"{context_block}\n\n"
        f"Compose the morning briefing from the context above. It is complete and "
        f"authoritative — do not ask for or assume any other data. Begin with the "
        f"established opener.\n\n"
        f"Compose the morning briefing for {today_label}.\n\n"
        f"{weather_line}\n\n"
        f"Today's scheduled events:\n{_events_block(today_evts)}\n\n"
        f"Upcoming this week:\n{_upcoming_block(upcoming_evts, today)}\n\n"
        f"Today's date is {today.isoformat()} ({today.strftime('%A')}).\n\n"
        f"Start with exactly: \"Good morning, sir. Here is your day:\"\n"
        f"Then in plain prose (no bullets, no markdown), 3-5 sentences:\n"
        f"  - Walk through today's events chronologically.\n"
        f"  - Surface upcoming items naturally — say \"tomorrow\", \"this Thursday\", "
        f"\"next Monday\" rather than \"in 3 days\". Emphasize anything close.\n"
        f"  - One short weather note if notable.\n"
        f"  - If today has no events, say so plainly and pivot to the week ahead."
    )
    return agent._think(prompt, use_tools=False)


def compose_evening(agent, bundle: dict) -> str:
    """Evening briefing from the pre-bundled context (see compose_morning)."""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    tomorrow_label = compat.strftime(tomorrow, "%A, %B %-d")
    tomorrow_evts = _safe_list(bundle.get("tomorrow_calendar"))
    upcoming_evts = _safe_list(bundle.get("week_ahead"))
    canvas_pending = _safe_list(bundle.get("canvas_pending"))
    weather_str = bundle.get("weather_tomorrow")
    weather_line = (
        f"Tomorrow's weather: {weather_str}"
        if weather_str and weather_str != _UNAVAILABLE else ""
    )

    context_block = format_briefing_context(bundle)
    prompt = (
        f"{context_block}\n\n"
        f"Compose the evening briefing from the context above. It is complete and "
        f"authoritative — do not ask for or assume any other data. Begin with the "
        f"established opener.\n\n"
        f"Compose the evening briefing. Looking ahead to {tomorrow_label}.\n\n"
        f"{weather_line}\n\n"
        f"Tomorrow's scheduled events:\n{_events_block(tomorrow_evts)}\n\n"
        f"Upcoming this week:\n{_upcoming_block(upcoming_evts, today)}\n\n"
        f"Pending Canvas items (SOON or URGENT, not yet alerted):\n"
        f"{_canvas_block(canvas_pending)}\n\n"
        f"Today's date is {today.isoformat()} ({today.strftime('%A')}).\n\n"
        f"Write in plain prose, no markdown, 3-5 sentences:\n"
        f"  - Preview tomorrow chronologically.\n"
        f"  - Refer to upcoming items by weekday or \"next <weekday>\" rather than "
        f"\"in N days\". Surface anything close-in or any pending Canvas URGENT/SOON.\n"
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
        f"Today ({compat.strftime(today, '%A, %B %-d')}):\n{_events_block(today_evts)}\n\n"
        f"Tomorrow ({compat.strftime(tomorrow, '%A, %B %-d')}):\n{_events_block(tomorrow_evts)}\n\n"
        f"Upcoming this week:\n{_upcoming_block(upcoming_evts, today)}\n\n"
        f"Pending Canvas items (SOON or URGENT):\n{_canvas_block(canvas_pending)}\n\n"
        f"Today's date is {today.isoformat()} ({today.strftime('%A')}).\n\n"
        f"Plain prose, no markdown, 3-5 sentences. Cover today, tomorrow, and "
        f"any upcoming items worth flagging — refer to them by weekday or \"next "
        f"<weekday>\" rather than \"in N days\". Include any pending Canvas urgency. "
        f"If everything is empty, say so plainly."
    )
    return agent._think(prompt, use_tools=False)
