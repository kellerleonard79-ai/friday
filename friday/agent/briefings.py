"""
agent/briefings.py
Briefing and alert prompt composers + deterministic context bundling.

TORN DOWN: the composers no longer make an LLM call. They render the bundle
as plain labeled text. Context bundling below is untouched and is the layer
this teardown is preserving.

Context bundling (bundle_briefing_context / format_briefing_context):
    A briefing needs a known-complete dataset. Rather than leave the model to
    decide whether to call tools, we pre-fetch everything a briefing needs
    (calendar, Canvas, weather, briefing-visible GroupMe) and inject it as a
    structured block at the top of the prompt. The model has no discretion
    over what data to pull, so a weak briefing is always a bundle problem. If
    a briefing turns up thin, expand the bundler.

    The tool layer is gone entirely as of the llm-layer-teardown branch, so
    "briefings run with tools off" is now true of every call Friday makes.
    The bundler is the layer that survives the rewrite.

Synchronous — wrap in run_in_executor when called from async.
"""

import logging
import re
from datetime import date, datetime, timedelta

import clock
import compat
from calendars import backend as calendar_backend
from connectors import weather as weather_connector

logger = logging.getLogger("friday.briefings")

# Sentinel stored in a bundle field when its fetch failed. The formatter renders
# it verbatim so a thin briefing is visible in both the message and the logs.
_UNAVAILABLE = "unavailable"

# How far back to look for GroupMe chatter worth surfacing (high + normal tiers).
_GROUPME_WINDOW_HOURS = 12

# The header lines connectors/groupme.py prepends to every stored body.
_GROUPME_HEADER = re.compile(r"^(Group|From):\s")


def _local_now(config: dict) -> datetime:
    """Authoritative current datetime in the configured timezone.

    Thin alias for clock.local_now — the rule (configured timezone beats the
    host clock, or the weekday label drifts by a day) moved to clock.py so the
    dispatcher could share it without importing this module. Kept as a name
    because every "today" in this file goes through it.
    """
    return clock.local_now(config)


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


def _event_date(evt: dict):
    """The local calendar date an event starts on, or None if unparseable."""
    dt = _local(evt.get("start_iso", ""))
    return dt.date() if dt else None


def _events_on(events: list[dict], day) -> list[dict]:
    """Slice a pre-fetched window down to one day — see the on_demand branch of
    bundle_briefing_context for why the window is fetched whole."""
    return [e for e in events if _event_date(e) == day]


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


def _fetch_groupme_surfaced(conn):
    """Briefing-visible GroupMe rows from the events buffer in the last 12h —
    the 'high' and 'normal' tiers. 'muted' rows stay in the buffer for history
    but never reach a briefing. Reads the table the connector already
    populated — no live API call here."""
    try:
        cutoff = (datetime.now().astimezone() - timedelta(hours=_GROUPME_WINDOW_HOURS)).isoformat()
        rows = conn.execute(
            "SELECT body, created_at FROM events "
            "WHERE source='groupme' AND created_at >= ? "
            "AND (body LIKE '%[priority=high]%' OR body LIKE '%[priority=normal]%') "
            "ORDER BY created_at",
            (cutoff,),
        ).fetchall()
        return [_parse_groupme_row(body, created_at) for body, created_at in rows]
    except Exception as e:
        logger.warning(f"Briefing bundle: groupme_surfaced fetch failed — {e}")
        return _UNAVAILABLE


def bundle_briefing_context(slot: str, config: dict, conn) -> dict:
    """Pre-fetch everything the briefing needs, each source guarded individually.
    Always returns a dict — a single failed fetch yields the _UNAVAILABLE
    sentinel for that field, never an aborted briefing.

    Morning and evening differ only by their date windows:
      morning → today + next 3 days (excluding today), weather today
      evening → tomorrow + next 5 days (from tomorrow), weather tomorrow
    """
    now = _local_now(config)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    bundle: dict = {"slot": slot, "now": now}

    if slot == "on_demand":
        # Asked for at an arbitrary hour, so it covers both ends: what is left
        # of today, what tomorrow holds, and the rest of the week.
        #
        # One read sliced three ways, rather than three reads. The user is
        # waiting on this one (it is the dashboard's Brief button), and a
        # calendar read is the slowest thing in the bundle by a wide margin —
        # on the JXA fallback each one can take a minute or more.
        window = _fetch_calendar_window(
            config, today, today + timedelta(days=7), "on_demand_calendar")
        if window is _UNAVAILABLE:
            bundle["today_calendar"] = _UNAVAILABLE
            bundle["tomorrow_calendar"] = _UNAVAILABLE
            bundle["week_preview"] = _UNAVAILABLE
        else:
            bundle["today_calendar"] = _events_on(window, today)
            bundle["tomorrow_calendar"] = _events_on(window, tomorrow)
            after = tomorrow + timedelta(days=1)
            bundle["week_preview"] = [
                e for e in window if (d := _event_date(e)) and d >= after
            ]
        bundle["weather_today"] = _fetch_weather(config, "weather today", "weather_today")
    elif slot == "morning":
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
    bundle["groupme_surfaced"] = _fetch_groupme_surfaced(conn)

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
        f"{_count(bundle.get('groupme_surfaced'))} groupme surfaced"
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
        return "  Nothing worth surfacing."
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
    now_str = (
        compat.strftime(now, "%A, %B %-d, %Y, %-I:%M %p %Z")
        if isinstance(now, datetime) else "?"
    )

    parts = [
        "===== BRIEFING CONTEXT (deterministic, do not re-fetch) =====",
        f"Current date and time: {now_str}",
        "This date, time, and weekday are authoritative. Use them verbatim. Never "
        "infer, calculate, or guess the day of the week — it is given to you above.",
        "",
    ]
    if slot == "on_demand":
        parts += [
            "Today's calendar:",
            _block_day_events(bundle.get("today_calendar")),
            "",
            "Tomorrow's calendar:",
            _block_day_events(bundle.get("tomorrow_calendar")),
            "",
            "Rest of the week:",
            _block_week(bundle.get("week_preview")),
            "",
            "Canvas pending:",
            _block_canvas(bundle.get("canvas_pending")),
            "",
            "Weather today:",
            _block_weather(bundle.get("weather_today")),
            "",
        ]
    elif slot == "morning":
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
        f"GroupMe (high + normal priority, last {_GROUPME_WINDOW_HOURS}h):",
        _block_groupme(bundle.get("groupme_surfaced")),
        "===== END CONTEXT =====",
    ]
    return "\n".join(parts)


# ── Renderers (deterministic) ─────────────────────────────────────────────────
#
# TORN DOWN: these three used to compose an LLM prompt around the bundle and
# return the model's prose. They now render the bundle directly — labeled
# sections, one line per item, no voice. The bundling above is the layer that
# survives; this is a placeholder so the daemon keeps delivering briefings
# while the prompt layer is rebuilt.
#
# Each keeps its (agent, bundle) signature so friday.py's run_in_executor call
# sites are unchanged. `agent` is unused and deliberately still there: the
# rewrite puts a model back on this seam.


def _render_sections(sections: list[tuple[str, str]]) -> str:
    """[(label, block)] → the labeled body. Blocks come from the _block_*
    helpers above, so a failed fetch renders as "unavailable" rather than
    silently reading as an empty day."""
    out = []
    for label, block in sections:
        out.append(f"{label}:")
        out.append(block)
        out.append("")
    return "\n".join(out).rstrip()


def _header(bundle: dict, title: str) -> str:
    now = bundle.get("now")
    when = (
        compat.strftime(now, "%A, %B %-d, %Y, %-I:%M %p %Z")
        if isinstance(now, datetime) else "?"
    )
    return f"{title} — {when}"


def _groupme_section(bundle: dict) -> tuple[str, str]:
    return (
        f"GroupMe (high + normal priority, last {_GROUPME_WINDOW_HOURS}h)",
        _block_groupme(bundle.get("groupme_surfaced")),
    )


def compose_morning(agent, bundle: dict) -> str:
    """Morning briefing, rendered straight from the bundle."""
    return "\n\n".join([
        _header(bundle, "MORNING BRIEFING"),
        _render_sections([
            ("Today's calendar", _block_day_events(bundle.get("today_calendar"))),
            ("Week ahead (next 3 days)", _block_week(bundle.get("week_preview"))),
            ("Canvas pending", _block_canvas(bundle.get("canvas_pending"))),
            ("Weather today", _block_weather(bundle.get("weather_today"))),
            _groupme_section(bundle),
        ]),
    ])


def compose_evening(agent, bundle: dict) -> str:
    """Evening briefing, rendered straight from the bundle."""
    return "\n\n".join([
        _header(bundle, "EVENING BRIEFING"),
        _render_sections([
            ("Tomorrow's calendar", _block_day_events(bundle.get("tomorrow_calendar"))),
            ("Week ahead (next 5 days)", _block_week(bundle.get("week_ahead"))),
            ("Canvas pending", _block_canvas(bundle.get("canvas_pending"))),
            ("Weather tomorrow", _block_weather(bundle.get("weather_tomorrow"))),
            _groupme_section(bundle),
        ]),
    ])


def compose_on_demand(agent, bundle: dict) -> str:
    """On-demand briefing (dashboard button, menubar), rendered straight from
    the bundle. The caller must still not record it as a briefing_sent, or the
    real scheduled one is skipped."""
    return "\n\n".join([
        _header(bundle, "BRIEFING"),
        _render_sections([
            ("Today's calendar", _block_day_events(bundle.get("today_calendar"))),
            ("Tomorrow's calendar", _block_day_events(bundle.get("tomorrow_calendar"))),
            ("Rest of the week", _block_week(bundle.get("week_preview"))),
            ("Canvas pending", _block_canvas(bundle.get("canvas_pending"))),
            ("Weather today", _block_weather(bundle.get("weather_today"))),
            _groupme_section(bundle),
        ]),
    ])


# ── Urgent alerts ─────────────────────────────────────────────────────────────
#
# TORN DOWN: compose_urgent_alert asked the model to announce the item in
# Friday's voice. Every urgent alert now takes the deterministic path below,
# which was already the model-unavailable fallback.


def _strip_groupme_scaffolding(body: str) -> str:
    """Drop the connector's leading "Group: …/From: …" lines (connectors/groupme.py).
    They exist to give the model context; the title already carries both, so
    repeating them to the user is the redundancy this whole path removes."""
    lines = (body or "").splitlines()
    i = 0
    while i < len(lines) and (
        not lines[i].strip() or _GROUPME_HEADER.match(lines[i])
    ):
        i += 1
    return "\n".join(lines[i:]).strip()


def fallback_urgent_alert(source: str, title: str, body: str) -> str:
    """Deterministic alert text for when the model is unavailable. Plainer than
    the composed version, but never an emoji-and-header dump — an LLM outage
    must not change what an alert looks like more than it has to."""
    title = (title or "").strip()
    if (source or "").strip() == "groupme":
        text = _strip_groupme_scaffolding(body)[:300]
        group, _, sender = title.partition(": ")
        where = f"in {group}" if group else "in GroupMe"
        who   = f" from {sender}" if sender else ""
        return f"Sir, new message {where}{who}: {text}".rstrip(": ").strip()
    body = (body or "").strip()[:300]
    lead = f"Sir, {title}" if title else "Sir, an urgent item just arrived."
    return f"{lead}\n{body}".strip()
