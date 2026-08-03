"""
agent/tools.py
Synchronous tool functions Gemini can invoke. The google-genai SDK auto-handles
the call loop when these are passed as `tools=[...]` to generate_content.

Each tool:
  - Has a clear docstring (Gemini's tool description)
  - Has typed parameters (Gemini's schema inference uses annotations)
  - Returns a JSON-serializable dict — never raises
"""

import functools
import inspect
import json
import logging
import time
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import compat
import memory.activity as activity
import memory.state as state
import self_edit
from calendars import backend as calendar_backend
from connectors import location, weather

logger = logging.getLogger("friday.tools")

# Names of the two recurring briefing jobs. friday.py registers them under
# these names so update_setting can replace them in place; keeping the strings
# here means the writer and the registrant cannot drift.
MORNING_BRIEFING_JOB = "morning_briefing_daily"
EVENING_BRIEFING_JOB = "evening_briefing_daily"


def make_tools(conn, config, agent=None):
    """Return the list of tool callables bound to this Friday instance.

    `agent` is the FridayAgent; the reschedule tool reads `agent.job_queue`
    and `agent._{morning,evening}_briefing_runner`, which friday.py attaches
    after the PTB Application is built. Tool calls happen at message time,
    long after that wiring is in place."""

    tz_name = (config.get("agent") or {}).get("timezone", "America/Chicago")
    local_tz = ZoneInfo(tz_name)

    def _instrument(fn):
        """Wrap a tool callable so every invocation writes a tool_calls row
        (name, args, result preview, duration, triggered_by). functools.wraps
        preserves __name__/__doc__/__annotations__/__wrapped__ so google-genai's
        signature- and annotation-based schema inference is unaffected. Recording
        is best-effort and never alters the tool's return value."""
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            result = fn(*args, **kwargs)
            try:
                try:
                    bound = sig.bind(*args, **kwargs)
                    bound.apply_defaults()
                    args_dict = dict(bound.arguments)
                except TypeError:
                    args_dict = {"args": list(args), "kwargs": kwargs}
                activity.record_tool_call(
                    conn,
                    tool_name=fn.__name__,
                    args_json=json.dumps(args_dict, default=str)[:1000],
                    result_preview=json.dumps(result, default=str),
                    duration_ms=int((time.monotonic() - start) * 1000),
                    triggered_by=getattr(agent, "_tool_triggered_by", "unknown"),
                )
            except Exception as e:
                logger.debug(f"tool instrumentation failed for {fn.__name__}: {e}")
            return result

        return wrapper

    def _to_local(iso_utc: str) -> str:
        """Calendar backends emit ISO strings (UTC '...Z' from Apple/JXA,
        offset-aware RFC3339 from Google). Convert to the user's local
        timezone so the LLM doesn't misread them as local."""
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
            "human":       compat.strftime(now, "%A, %B %-d %Y, %-I:%M %p %Z"),
        }

    def get_schedule(start_date: str, end_date: str) -> dict:
        """Return events from the user's calendar with start times in
        [start_date, end_date). Both dates must be ISO YYYY-MM-DD; end_date is
        exclusive. Only whitelisted calendars are included. Use this for any
        schedule, calendar, appointment, or 'what am I doing' question.
        For a single day pass end_date = start_date + 1.

        Times in returned events are already in the user's local timezone —
        present them to the user as-is. Do not apply additional offsets.

        Each event carries a `uid`. That is the handle for
        update_calendar_event — call this tool first whenever the user wants to
        change an event that already exists. Never show a uid to the user."""
        try:
            s = date.fromisoformat(start_date)
            e = date.fromisoformat(end_date)
        except ValueError as err:
            return {"error": f"Invalid date format: {err}", "events": []}
        raw = calendar_backend.events_in_window(config, s, e)
        events = [{
            "title":    ev.get("title", ""),
            "start":    _to_local(ev.get("start_iso", "")),
            "end":      _to_local(ev.get("end_iso", "")),
            "location": ev.get("location", ""),
            "calendar": ev.get("calendar", ""),
            "uid":      ev.get("uid", ""),
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

    def get_location() -> dict:
        """Return where the machine running Friday currently is. Use for "where
        am I", "what's my location", "what city am I in".

        This is the location of the always-on Mac, NOT of the user's phone —
        it is only the user's location while they are with the machine. Never
        claim to know where the user personally is.

        `source` sets how precisely you may speak:
          - "corelocation": device positioning, accurate to `accuracy_m` metres.
            State the place plainly.
          - "ip": derived from the internet connection, city-level at best and
            sometimes off by a city. Hedge — "your connection places the Mac
            in ...".
        On {"error": ...} say the location could not be determined; do not
        guess a city from anything else in the conversation."""
        fix = location.fetch()
        if fix is None:
            return {"error": "location unavailable"}
        return {
            "place":      fix.get("place") or "",
            "latitude":   round(fix["lat"], 4),
            "longitude":  round(fix["lon"], 4),
            "accuracy_m": fix.get("accuracy_m"),
            "source":     fix["source"],
        }

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

    def reschedule_briefing(briefing_type: str, time: str) -> dict:
        """Override the next occurrence of a scheduled briefing. Use this when
        the user wants to move WHEN they receive their morning or evening
        briefing — applies only to the very next firing, then the default
        schedule from config is restored automatically.

        Args:
            briefing_type: "morning" or "evening".
            time: 24-hour HH:MM, e.g. "09:00", "21:30". Convert any phrasing
                  the user gives ("9 AM", "nine in the morning", "half past
                  eight") to HH:MM yourself before calling.

        If the requested time has already passed today, it is interpreted
        as that time tomorrow. Returns the resolved fire time in the user's
        local timezone — relay it to the user when confirming."""
        if briefing_type not in ("morning", "evening"):
            return {"status": "error",
                    "message": "briefing_type must be 'morning' or 'evening'"}
        try:
            hh, mm = [int(x) for x in time.split(":")]
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError
        except (ValueError, AttributeError):
            return {"status": "error",
                    "message": f"Invalid time '{time}', expected HH:MM 24-hour"}

        now_local = datetime.now(local_tz)
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now_local:
            target += timedelta(days=1)

        state.set(conn, f"{briefing_type}_briefing_override", target.isoformat())

        jq     = getattr(agent, "job_queue", None)
        runner = getattr(agent, f"_{briefing_type}_briefing_runner", None)
        if jq is None or runner is None:
            logger.error("reschedule_briefing called before scheduler attached")
            return {"status": "error",
                    "message": "Scheduler not yet available; try again in a moment."}

        job_name = f"{briefing_type}_briefing_override_job"
        for j in jq.get_jobs_by_name(job_name):
            j.schedule_removal()
        jq.run_once(runner, when=target, name=job_name)

        logger.info(
            f"Override set: {briefing_type} briefing at "
            f"{target.strftime('%Y-%m-%d %H:%M %Z')}"
        )
        return {
            "status": "ok",
            "type": briefing_type,
            "scheduled_for_local": compat.strftime(target, "%A, %B %-d at %-I:%M %p %Z"),
            "timezone": tz_name,
        }

    def _quip_for(context: str) -> str:
        """Pick a personality quip for an action confirmation. Reloaded from
        disk every call — edit quips.yaml and the next action picks it up
        without a restart."""
        import phrases
        prompt, quips = phrases.quip_prompt(context)
        raw = agent._think(prompt, use_tools=False,
                           triggered_by="user_message") if agent else ""
        return phrases.pick_quip(raw, quips)

    def add_calendar_event(
        title: str,
        date: str,
        start_time: str = "",
        end_time: str = "",
        calendar: str = "",
        location: str = "",
        notes: str = "",
    ) -> dict:
        """Write a NEW event to the user's calendar immediately, with no
        approval gate. Friday sends the user a one-line confirmation with a
        personality quip appended ("Dentist added for tomorrow at 2:00 PM.
        Your wish is my... mild inconvenience.") on success — you do NOT
        need to describe the event again afterward.

        Use this for work shifts, appointments, meetings — anything the user
        says they have coming up. Always call get_now() first to resolve
        relative phrases like 'next Saturday', 'tomorrow', 'this Friday'.

        Only for events that do not exist yet. If the user is adding a detail
        to, correcting, or moving something already on the calendar, use
        update_calendar_event instead — calling this tool would leave them
        with two copies of the same event.

        Args:
            title:      Short event title, e.g. "Work — Nation" or "Dentist".
                        REQUIRED before calling: (1) sanity-check the words —
                        if a word looks like a transcription error or typo
                        ("git apples", "by milk", "dock tor"), correct it to
                        the obvious intended word ("Get Apples", "Buy Milk",
                        "Doctor"). When the intended word is genuinely
                        ambiguous, ask the user instead of guessing.
                        (2) Title Case the result — capitalize the first
                        letter of every significant word. Never pass an
                        all-lowercase or all-uppercase title. Preserve
                        intentional internal casing (iPhone, FBLA).
            date:       ISO YYYY-MM-DD.
            start_time: 24-hour HH:MM (e.g. "07:00", "14:30"). Convert any
                        phrasing ("7 AM", "noon", "two thirty PM") to HH:MM
                        before calling. Omit for an all-day event.
            end_time:   24-hour HH:MM. Required for ranged events like shifts;
                        omit to default to a 1-hour duration. Must be after
                        start_time on the same day (overnight ranges not yet
                        supported).
            calendar:   Optional calendar name. Omit to use the user's
                        configured default calendar.
            location:   Optional place the event happens — a venue, address,
                        or room ("Gulf Breeze", "Dr. Patel's office"). This is
                        the calendar's Location field; do not put places in
                        notes.
            notes:      Optional free-text notes.
        """
        handler = getattr(agent, "telegram_handler", None) if agent else None
        if handler is None:
            return {"status": "error",
                    "message": "Calendar writer not available"}
        from actions import calendar as cal_action
        event = {"title": title, "date": date}
        if start_time:
            event["start_time"] = start_time
        if end_time:
            event["end_time"] = end_time
        if calendar:
            event["calendar"] = calendar
        if location:
            event["location"] = location
        if notes:
            event["notes"] = notes
        # Pick the quip first so the confirmation ships as a single message.
        try:
            day_str = compat.strftime(datetime.strptime(date, "%Y-%m-%d"), "%A %B %-d")
        except ValueError:
            day_str = date
        context_parts = [f"Just added '{title}' to the calendar for {day_str}"]
        if start_time:
            context_parts.append(f"at {start_time}")
        quip = _quip_for(" ".join(context_parts) + ".")
        default_cal = (config.get("agent") or {}).get("default_calendar")
        uid = cal_action.auto_write(
            event, telegram=handler, default_calendar=default_cal, quip=quip,
        )
        if not uid:
            # auto_write already sent the user a failure note in this case.
            return {"status": "error",
                    "message": "Calendar write failed — see logs"}
        # Flag the handler that the confirmation message already went out, so
        # an empty LLM follow-up isn't treated as a failure and we don't
        # double-message the user. Stash the exact text we sent so the
        # Telegram layer can write it into conversation_history — past rows
        # train the LLM's future output, so the row must match what shipped.
        if agent is not None:
            agent._last_action_emitted = "calendar_added"
            agent._last_calendar_confirmation = cal_action.format_confirmation(
                event, quip,
            )
        return {
            "status": "added",
            "apple_event_uid": uid,
            "user_already_sees": "a one-line confirmation message from Friday naming the event, day, and time, with a quip appended",
            "next_action": "Do not produce any further output for this turn. Friday has already replied.",
        }

    def update_calendar_event(
        uid: str,
        title: str = "",
        date: str = "",
        start_time: str = "",
        end_time: str = "",
        calendar: str = "",
        location: str = "",
        notes: str = "",
    ) -> dict:
        """Edit an event that is ALREADY on the user's calendar, in place and
        immediately, with no approval gate. Friday sends the user a one-line
        confirmation naming what changed, with a quip appended — you do NOT
        need to describe the change again afterward.

        Use this whenever the user amends something they already told you
        about: adding a location, fixing a name, moving a time, changing the
        end time. Anything that follows an event you just created ("the
        location for that is...", "actually make it 8", "that's at the other
        campus") is an edit, not a new event. Adding the event again would
        leave the user with duplicates.

        How to call it:
          1. Call get_schedule() covering the day the event is on and find it.
          2. Pass its `uid` and `calendar` from that result, plus ONLY the
             fields that change.
        Never invent a uid — if get_schedule doesn't return the event, tell the
        user you can't find it rather than guessing or adding a new one.

        Args:
            uid:        REQUIRED. The event's `uid` from get_schedule.
            title:      New title, Title Cased, same hygiene rules as
                        add_calendar_event. Omit to leave the title alone.
            date:       ISO YYYY-MM-DD, the day the event should end up on.
                        REQUIRED whenever you change start_time or end_time —
                        pass the event's existing date if only the time is
                        moving. Omit entirely if the timing isn't changing.
            start_time: New 24-hour HH:MM start. Requires `date`.
            end_time:   New 24-hour HH:MM end. Requires `date` and start_time;
                        must be later than start_time on the same day.
            calendar:   The calendar the event lives on, from get_schedule.
                        Optional, but pass it whenever you have it — without
                        it the lookup has to scan every calendar.
            location:   New location — a venue, address, or room. This is the
                        calendar's Location field; never put a place in notes.
            notes:      New free-text notes. Replaces any existing notes.

        Omitted fields are left untouched. To deliberately clear a field, pass
        a single space rather than leaving it out.
        """
        handler = getattr(agent, "telegram_handler", None) if agent else None
        if handler is None:
            return {"status": "error", "message": "Calendar writer not available"}
        if not uid:
            return {"status": "error",
                    "message": "uid required — call get_schedule first to find the event"}
        if (start_time or end_time) and not date:
            return {"status": "error",
                    "message": "date is required when changing start_time or end_time"}

        from actions import calendar as cal_action
        # "" means "not supplied" over the wire, since the SDK can't send None;
        # a lone space is the escape hatch for deliberately clearing a field.
        def _opt(v: str) -> str | None:
            return None if v == "" else v.strip()

        changed = [n for n, v in (("title", title), ("time", start_time or end_time),
                                  ("location", location), ("notes", notes)) if v]
        quip = _quip_for(
            f"Just updated the calendar event '{title or 'that event'}' — "
            f"changed its {', '.join(changed) if changed else 'details'}."
        )
        result = cal_action.auto_update(
            uid,
            telegram=handler,
            quip=quip,
            calendar=calendar,
            title=_opt(title),
            date=_opt(date),
            start_time=_opt(start_time),
            end_time=_opt(end_time),
            location=_opt(location),
            notes=_opt(notes),
        )
        if not result:
            # auto_update already sent the user a failure note.
            return {"status": "error",
                    "message": "Calendar update failed — see logs"}
        # Same contract as add_calendar_event: the confirmation already shipped,
        # so flag the handler to drop any residual model output and log the
        # exact text the user saw.
        if agent is not None:
            agent._last_action_emitted = "calendar_added"
            agent._last_calendar_confirmation = result.get("confirmation", "")
        return {
            "status": "updated",
            "apple_event_uid": result.get("uid", ""),
            "user_already_sees": "a one-line confirmation message from Friday naming the event and what changed, with a quip appended",
            "next_action": "Do not produce any further output for this turn. Friday has already replied.",
        }

    # ── Self-editing ─────────────────────────────────────────────────────
    # Friday's own voice and a short whitelist of settings. All of it takes
    # effect on the next message — nothing here needs a restart, and nothing
    # here can touch Friday's Python source.

    def add_quip(text: str, target: str = "both") -> dict:
        """Teach Friday a new phrase to use from time to time. Use this when
        the user says things like 'add "..." as a quip', 'start saying "..."',
        'remember to say "..." now and then', or 'add that to your phrases'.

        Args:
            text:   The phrase, EXACTLY as the user gave it. Do not paraphrase
                    it, do not fix its grammar, do not add or remove "sir",
                    do not soften the joke. If it arrived through voice
                    transcription and contains an obvious mis-hearing, ask the
                    user to confirm the wording rather than guessing. Pass it
                    without surrounding quotation marks.
            target: Where the phrase should show up.
                    "confirmation" — only after a calendar event is added or
                                     updated. Use when the user ties it to
                                     scheduling ("say that when you add
                                     something to my calendar").
                    "voice"        — only in ordinary conversation.
                    "both"         — the default. Use when the user just says
                                     "add this as a quip" with no context.

        The phrase is live immediately. Never tell the user to restart you or
        that it will apply later — confirm briefly and move on.
        """
        return self_edit.add_phrase(text, target)

    def list_quips(target: str = "both") -> dict:
        """List the phrases Friday can currently draw on. Use for 'what quips
        do you have', 'what phrases do you know', 'read back my quips'.

        Returns them split into `learned` (added by the user, removable) and
        `bundled` (shipped with Friday), plus `disabled` — bundled phrases the
        user has retired. When reading them back, give the phrases themselves;
        the user does not need the bundled/learned distinction unless they ask
        to remove one.
        """
        result = self_edit.list_phrases()
        t = (target or "both").lower()
        if t == "confirmation":
            return {"confirmation": result["confirmation"]}
        if t == "voice":
            return {"voice": result["voice"]}
        return result

    def remove_quip(text: str, target: str = "both") -> dict:
        """Stop Friday using a phrase. Use for 'drop that quip', 'stop saying
        the touch grass one', 'remove "..." from your phrases'.

        Args:
            text:   Enough of the phrase to identify it — a distinctive few
                    words is enough, matching is case-insensitive and partial.
            target: "confirmation", "voice", or "both" (default).

        On status "ambiguous" the tool has NOT removed anything: read the
        `matches` back to the user and ask which one they mean. On "not_found",
        say plainly that no phrase matches rather than guessing at a near miss.
        """
        return self_edit.remove_phrase(text, target)

    def update_setting(key: str, value: str) -> dict:
        """Change one of Friday's own settings, permanently. Use when the user
        wants Friday itself to behave differently from now on — not for
        one-off requests.

        Args:
            key: One of exactly these, and nothing else:
                 "persona.snark_level"         — "none", "medium", "maximum".
                 "persona.preset"              — "professional", "butler",
                                                 "friday".
                 "persona.custom_instructions" — free-text standing
                                                 instructions Friday should
                                                 always follow. This REPLACES
                                                 whatever was there before, so
                                                 if the user is adding to
                                                 existing instructions rather
                                                 than rewriting them, confirm
                                                 the full new text first.
                 "agent.default_calendar"      — calendar new events go to.
                 "agent.morning_briefing_time" — 24-hour "HH:MM".
                 "agent.briefing_time"         — evening briefing, "HH:MM".
            value: The new value, converted to the form above. Turn "half past
                   seven in the morning" into "07:30" yourself.

        Any other key is refused — API keys, tokens, models, and file paths are
        edited in the dashboard only. When refused, tell the user that plainly
        and point them at the dashboard; do not look for another way to do it.

        For a ONE-TIME change to when a briefing arrives ("brief me at 9
        tomorrow"), use reschedule_briefing instead — this tool changes the
        standing schedule.

        Changes apply immediately. Never tell the user to restart you.
        """
        result = self_edit.update_setting(key, value, live_config=config)
        if not result.get("ok") or not key.strip().endswith("briefing_time"):
            return result

        # The config file now says 07:30, but the job_queue still holds the
        # old daily job. Replace it, or the setting would not take effect
        # until the next restart.
        which   = "morning" if key.strip().endswith("morning_briefing_time") else "evening"
        job_name = MORNING_BRIEFING_JOB if which == "morning" else EVENING_BRIEFING_JOB
        jq      = getattr(agent, "job_queue", None)
        runner  = getattr(agent, f"_{which}_briefing_runner", None)
        if jq is None or runner is None:
            logger.error("update_setting called before scheduler attached")
            result["warning"] = "Saved, but the new time takes effect after a restart."
            return result
        hh, mm = (int(x) for x in result["value"].split(":"))
        for j in jq.get_jobs_by_name(job_name):
            j.schedule_removal()
        jq.run_daily(runner, time=dtime(hh, mm, tzinfo=local_tz), name=job_name)
        logger.info(f"{which} briefing job re-registered for {result['value']}")
        result["rescheduled"] = True
        return result

    return [_instrument(fn) for fn in (
        get_now, get_schedule, get_weather, get_location,
        get_pending_canvas, reschedule_briefing,
        add_calendar_event, update_calendar_event,
        add_quip, list_quips, remove_quip, update_setting,
    )]
