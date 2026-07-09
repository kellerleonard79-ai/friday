"""
friday.py
Project Friday — entry point.

PTB Application owns the main event loop.
All scheduling goes through job_queue — no secondary threads, no schedule library.
"""

import asyncio
import datetime
import json
import logging
import os
import re
import signal
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters

import compat
import paths

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(str(paths.log_dir() / "friday.log")),
    ],
)
logger = logging.getLogger("friday")

# Internal "[priority=high]" / "[priority=low]" tag the GroupMe connector prepends
# to each event body purely as a signal for the LLM's urgency pass and the
# briefing/urgent SQL LIKE queries. It must stay in events.body, but it is noise
# in any user-facing message — strip it (and its now-orphaned blank line) before
# the text reaches Telegram.
_PRIORITY_TAG = re.compile(r"^\[priority=[^\]]*\]\n?", re.MULTILINE)


def _strip_internal_tags(body: str) -> str:
    """Remove internal LLM-only tags from an event body for user-facing display."""
    return _PRIORITY_TAG.sub("", body).strip()


from agent.core import FridayAgent
from channels.telegram import TelegramHandler
from memory.db import Database
import memory.activity as activity
import memory.state as state
from calendars import backend as calendar_backend
from connectors import canvas as canvas_connector
from connectors import gcal_sync
from connectors import groupme as groupme_connector
from agent import briefings
from actions import calendar as apple_writer
from dashboard import server as dashboard_server


def load_config() -> dict:
    path = paths.config_path()
    if not path.exists():
        logger.critical(f"Config not found: {path}")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def check_environment(config: dict) -> None:
    errors = []
    tg = config.get("telegram", {})
    if not (tg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")):
        errors.append("telegram.bot_token not set.")
    if not (tg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID")):
        errors.append("telegram.chat_id not set.")
    if config.get("provider") == "gemini":
        if not (config.get("gemini", {}).get("api_key") or os.environ.get("GEMINI_API_KEY")):
            errors.append("GEMINI_API_KEY not set.")
    for e in errors:
        logger.error(f"Config error: {e}")
    if errors:
        sys.exit(1)


# ── Feature map (everything main() wires up) ──────────────────────────────────
#   • Config load + env validation + calendar-backend selection
#   • SQLite DB open, FridayAgent (LLM), TelegramHandler (semaphore entry point)
#   • Dashboard web server started inside this same asyncio loop
#   • Scheduled jobs registered on PTB JobQueue:
#       run_daily    → morning_briefing_job   (08:00-ish, tz-window guarded)
#       run_daily    → briefing_job (evening)  (21:45-ish, tz-window guarded)
#       run_repeating→ poll_connectors_job     (15 min: Canvas, gcal_sync,
#                                                GroupMe, then LLM tagging +
#                                                GroupMe event extraction)
#       run_repeating→ check_urgent_alerts_job  (1 min: fire URGENT interrupts)
#   • One-shot briefing overrides (reschedule tool) + missed-briefing catch-up
#   • run_polling owns the only event loop (no second scheduler, no threads)
def main() -> None:
    logger.info("=" * 50)
    logger.info("  Project Friday — Starting up")
    logger.info(f"  {datetime.datetime.now().strftime('%A, %B %d %Y %H:%M')}")
    logger.info("=" * 50)

    config = load_config()
    check_environment(config)
    calendar_backend.init(config)

    db   = Database(str(paths.db_path(config)))
    conn = db.connection()

    agent   = FridayAgent(config, conn=conn)
    handler = TelegramHandler(config, agent, conn)
    agent.telegram_handler = handler  # late-bound for propose_calendar_event tool

    bot_token  = handler.bot_token
    chat_id    = handler.chat_id
    agent_cfg  = config.get("agent", {})
    tz_name    = agent_cfg.get("timezone", "America/Chicago")
    local_tz   = ZoneInfo(tz_name)
    bt_str     = agent_cfg.get("briefing_time", "21:45")
    bh, bm     = (int(x) for x in bt_str.split(":"))
    mbt_str    = agent_cfg.get("morning_briefing_time", "08:00")
    mbh, mbm   = (int(x) for x in mbt_str.split(":"))

    # Sanity windows: a briefing fired well outside its expected hour almost
    # always means a timezone misconfiguration. Refuse to send rather than
    # ping the user at 2 AM.
    MORNING_WINDOW = (6, 10)   # [06:00, 10:00) local
    EVENING_WINDOW = (19, 24)  # [19:00, 24:00) local

    def _within(window: tuple[int, int]) -> bool:
        now_local = datetime.datetime.now(local_tz)
        return window[0] <= now_local.hour < window[1]

    # ── Post-init: startup message + initial state ────────────────────────────

    async def post_init(app: Application) -> None:
        state.set(conn, "status",     "running")
        state.set(conn, "started_at", datetime.datetime.now().isoformat())
        state.set(conn, "provider",   config.get("provider", "ollama"))
        state.set(conn, "model",      agent.model_name)
        # Honor pre-existing pause state across restarts; default to unpaused.
        if state.get(conn, "paused") is None:
            state.set(conn, "paused", "false")

        # Dashboard web server — runs inside this same asyncio loop. No threads,
        # no second event loop. Honors the single-loop rule in CLAUDE.md.
        config_path = paths.config_path()
        try:
            server = await dashboard_server.start_server(config_path, conn)
            task = asyncio.create_task(server.serve(), name="dashboard_server")
            app.bot_data["dashboard_server"] = server
            app.bot_data["dashboard_task"] = task
        except Exception as e:
            logger.error(f"Dashboard server failed to start: {e}")

        await app.bot.send_message(
            chat_id=chat_id,
            text=f"Friday online — {config.get('provider', 'ollama')} / {agent.model_name}",
        )
        logger.info("Startup complete.")

    # ── Post-stop: offline message ────────────────────────────────────────────

    async def post_stop(app: Application) -> None:
        state.set(conn, "status", "stopped")
        server = app.bot_data.get("dashboard_server")
        if server is not None:
            server.should_exit = True
        task = app.bot_data.get("dashboard_task")
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=3)
            except (asyncio.TimeoutError, Exception):
                pass
        try:
            await app.bot.send_message(chat_id=chat_id, text="Friday going offline.")
        except Exception:
            pass
        conn.close()

    # ── Briefing activity recorder ────────────────────────────────────────────
    # Records every briefing that actually shipped (body + how late it was) for
    # the dashboard Today feed. Derives on_time/catchup/override from the job
    # name and age from the configured slot time. Best-effort — wrapped so a
    # logging failure can never break the send path.
    def _record_briefing_sent(slot: str, body: str, job_name: str,
                              sched_h: int, sched_m: int) -> None:
        if job_name.endswith("catchup_job"):
            kind = "catchup"
        elif job_name.endswith("override_job"):
            kind = "override"
        else:
            kind = "on_time"
        now_local = datetime.datetime.now(local_tz)
        scheduled = now_local.replace(hour=sched_h, minute=sched_m,
                                      second=0, microsecond=0)
        age_min = max(0, int((now_local - scheduled).total_seconds() // 60))
        activity.record_briefing(conn, slot=slot, body=body,
                                 on_time_vs_catchup=kind, age_minutes=age_min)

    # ── Morning briefing job ─────────────────────────────────────────────────

    async def morning_briefing_job(context) -> None:
        job_name = getattr(context.job, "name", "") or ""
        is_override = job_name == "morning_briefing_override_job"
        is_catchup  = job_name == "morning_briefing_catchup_job"
        if is_override:
            state.delete(conn, "morning_briefing_override")
        elif not is_catchup:
            ovr = state.get(conn, "morning_briefing_override")
            if ovr:
                try:
                    ovr_dt = datetime.datetime.fromisoformat(ovr)
                    if ovr_dt.date() == datetime.datetime.now(local_tz).date():
                        logger.info(
                            f"Morning briefing skipped at default time — "
                            f"override active for {ovr_dt.strftime('%H:%M %Z')}"
                        )
                        return
                except ValueError:
                    pass
            if not _within(MORNING_WINDOW):
                now_local = datetime.datetime.now(local_tz)
                logger.warning(
                    f"Morning briefing fired at {now_local.strftime('%H:%M %Z')} — "
                    f"outside {MORNING_WINDOW[0]:02d}:00–{MORNING_WINDOW[1]:02d}:00 "
                    f"window. Skipping (likely tz misconfig)."
                )
                return
        today = datetime.date.today()
        # Claim the slot BEFORE the slow compose, not after the send. The 60s
        # catch-up net checks last_morning_briefing_sent every minute; if we
        # only locked after sending, a compose taking >60s would let the net
        # queue a second briefing while this one is still composing (duplicate).
        # is_catchup already holds the lock from _check_and_run_missed_briefings.
        if not is_catchup:
            if state.get(conn, "last_morning_briefing_sent") == today.isoformat():
                return  # already sent today
            state.set(conn, "last_morning_briefing_sent", today.isoformat())
        loop = asyncio.get_running_loop()
        # Pre-fetch a known-complete dataset (calendar/canvas/weather/groupme)
        # so the composer works from injected context instead of tool calls.
        bundle = await loop.run_in_executor(
            None, briefings.bundle_briefing_context, "morning", config, conn
        )
        response = await loop.run_in_executor(
            None, briefings.compose_morning, agent, bundle
        )
        if response:
            # On a late catch-up, swap the standard greeting for a plain note
            # that the machine was asleep. Emoji-free, since this can be spoken
            # via TTS. Only when meaningfully late (>20 min) — a few minutes off
            # keeps the normal opener.
            if is_catchup:
                now_local = datetime.datetime.now(local_tz)
                scheduled = now_local.replace(hour=mbh, minute=mbm,
                                              second=0, microsecond=0)
                late_min = int((now_local - scheduled).total_seconds() // 60)
                if late_min > 20:
                    late_opener = (
                        "Running a little late this morning, sir — the machine "
                        "was asleep until just now."
                    )
                    if "Good morning, sir." in response:
                        response = response.replace(
                            "Good morning, sir.", late_opener, 1
                        )
                    else:
                        response = f"{late_opener}\n\n{response}"
            try:
                # The morning composer's prompt already makes the body open with
                # "Good morning, sir. Here is your day:", so we only strip the
                # emoji here — prepending another opener would double the greeting.
                await context.bot.send_message(chat_id=chat_id, text=response)
                _record_briefing_sent("morning", response, job_name, mbh, mbm)
            except Exception as e:
                logger.error(f"Morning briefing send failed: {e}")
                # Release the slot so the catch-up net can retry a failed send.
                state.delete(conn, "last_morning_briefing_sent")
        else:
            # Empty compose isn't a real send — don't let the lock suppress retry.
            state.delete(conn, "last_morning_briefing_sent")

    # ── LLM urgency tagging for unprocessed events ───────────────────────────

    async def process_untagged_events(loop) -> None:
        rows = conn.execute(
            "SELECT id, title, body, due_at, source FROM events WHERE processed=0"
        ).fetchall()
        for event_id, title, body, due_at, source in rows:
            if source == "groupme":
                criteria = (
                    "GroupMe message. The body's first line is "
                    "[priority=high] or [priority=low].\n"
                    "URGENT = priority=high AND the message is genuinely "
                    "time-sensitive (emergency, ASAP request, "
                    "imminent deadline, direct urgent ask).\n"
                    "SOON   = priority=high AND mentions something "
                    "happening in the next few days.\n"
                    "NORMAL = everything else. "
                    "priority=low messages are never URGENT or SOON."
                )
            else:
                criteria = (
                    "URGENT = due within 24h, or exam/quiz/critical deadline.\n"
                    "SOON   = due within 3 days.\n"
                    "NORMAL = everything else or no due date."
                )
            prompt = (
                f"Event from {source}:\nTitle: {title}\nDue: {due_at}\n"
                f"Details: {(body or '')[:500]}\n\n"
                f"Assign urgency. Reply with exactly one word: URGENT, SOON, or NORMAL.\n"
                f"{criteria}"
            )
            urgency = await loop.run_in_executor(
                None, lambda: agent._think(prompt, use_tools=False, triggered_by="poll")
            )
            urgency = urgency.strip().upper()
            if urgency not in ("URGENT", "SOON", "NORMAL"):
                urgency = "NORMAL"
            conn.execute(
                "UPDATE events SET urgency=?, processed=1 WHERE id=?",
                (urgency, event_id),
            )
            logger.info(f"Tagged {event_id} as {urgency}")
        if rows:
            conn.commit()

    # ── Event extraction from groupme messages ───────────────────────────────

    async def extract_groupme_events(loop) -> None:
        """For high-priority groupme rows that haven't been examined, ask the
        LLM to extract a concrete calendar event and propose it via gated_write.
        Conservative — requires explicit date AND start time."""
        rows = conn.execute(
            "SELECT id, title, body FROM events "
            "WHERE source='groupme' AND event_extracted=0 "
            "AND body LIKE '%[priority=high]%'"
        ).fetchall()
        if not rows:
            return
        handler = getattr(agent, "telegram_handler", None)
        if handler is None:
            logger.warning("groupme event extraction: telegram handler not bound — skipping")
            return
        default_cal = (config.get("agent") or {}).get("default_calendar")
        today_local = datetime.datetime.now(local_tz).date()
        today_iso = today_local.isoformat()
        weekday   = today_local.strftime("%A")

        for row_id, title, body in rows:
            prompt = (
                f"A GroupMe message:\n\n{body}\n\n"
                f"Today is {today_iso} ({weekday}).\n\n"
                f"Does this message describe a SPECIFIC event the user should "
                f"add to their personal calendar — one with a clear date AND "
                f"clear start time?\n\n"
                f"Reply NONE if any of these are true:\n"
                f"- No explicit date or start time\n"
                f"- Vague phrasing (\"soon\", \"later\", \"sometime\", \"next week\" without a day)\n"
                f"- The message discusses a past event\n"
                f"- General chatter, questions, opinions, banter\n"
                f"- Voting / scheduling poll where the time is still open\n"
                f"- A recurring routine the user obviously already knows\n\n"
                f"If there IS a concrete event, reply with ONLY a single-line JSON "
                f"object (no markdown, no prose):\n"
                f'{{"title":"...","date":"YYYY-MM-DD","start_time":"HH:MM",'
                f'"end_time":"HH:MM or null","notes":"..."}}\n\n'
                f"Rules:\n"
                f"- title under 50 chars, descriptive\n"
                f"- date AND start_time are required; if either is ambiguous, reply NONE\n"
                f"- end_time only if explicitly stated\n"
                f"- convert relative dates to absolute YYYY-MM-DD using the today date above\n"
                f"- notes: short, include any location mentioned\n"
            )
            raw = await loop.run_in_executor(
                None, lambda p=prompt: agent._think(p, use_tools=False, triggered_by="poll")
            )
            raw = (raw or "").strip()
            # Mark scanned regardless of outcome so we never retry the same row
            conn.execute(
                "UPDATE events SET event_extracted=1 WHERE id=?", (row_id,)
            )
            conn.commit()

            if not raw or raw.upper() == "NONE":
                continue
            # Strip ```json fences the LLM may have added despite instructions
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                logger.info(
                    f"groupme event extraction: unparseable LLM output for {row_id}: {raw[:200]}"
                )
                continue
            if not isinstance(event, dict):
                continue
            # Normalise LLM "null" string for end_time
            if str(event.get("end_time", "")).strip().lower() in ("null", "none", ""):
                event.pop("end_time", None)

            # Prepend source attribution to the notes field so the card shows
            # which group surfaced the event.
            group_name = ""
            for line in (body or "").splitlines():
                if line.startswith("Group: "):
                    group_name = line[len("Group: "):].strip()
                    break
            source_line = (
                f"From GroupMe: {group_name}" if group_name else "From GroupMe"
            )
            existing_notes = (event.get("notes") or "").strip()
            event["notes"] = source_line + (
                "\n" + existing_notes if existing_notes else ""
            )

            try:
                pending = apple_writer.gated_write(
                    event, conn, handler, default_calendar=default_cal,
                )
            except Exception as e:
                logger.error(f"groupme event extraction: gated_write failed for {row_id}: {e}")
                continue
            if pending:
                logger.info(
                    f"groupme event extraction: proposed for {row_id} — "
                    f"{event.get('title')!r} on {event.get('date')}"
                )

    # ── Poll connectors job ───────────────────────────────────────────────────

    async def poll_connectors_job(context) -> None:
        logger.info("Polling connectors...")
        loop = asyncio.get_running_loop()

        canvas_cfg = config.get("canvas", {})
        if canvas_cfg.get("ical_url"):
            try:
                count = await loop.run_in_executor(
                    None, canvas_connector.fetch, canvas_cfg, conn
                )
                if count:
                    logger.info(f"Canvas: {count} new event(s) written.")
                else:
                    logger.info("Canvas: no new events.")
                synced = await loop.run_in_executor(
                    None, canvas_connector.sync_to_calendar, config, conn,
                )
                if synced:
                    logger.info(f"Canvas: {synced} due date(s) written to calendar.")
            except Exception as e:
                logger.error(f"Canvas poll failed: {e}")

        gcal_cfg = config.get("gcal_sync") or {}
        # gcal_sync mirrors Google → Apple; meaningless when Google Calendar
        # already IS the event store (Windows / google backend).
        if gcal_cfg.get("calendars") and calendar_backend.backend_name(config) == "apple":
            try:
                count = await loop.run_in_executor(
                    None, gcal_sync.fetch, config, conn
                )
                if count:
                    logger.info(f"gcal_sync: {count} total new event(s).")
            except Exception as e:
                logger.error(f"gcal_sync poll failed: {e}")

        groupme_cfg = config.get("groupme") or {}
        if groupme_cfg.get("api_token") and groupme_cfg.get("groups"):
            try:
                count = await loop.run_in_executor(
                    None, groupme_connector.fetch, groupme_cfg, conn,
                )
                if count:
                    logger.info(f"GroupMe: {count} new message(s) written.")
            except Exception as e:
                logger.error(f"GroupMe poll failed: {e}")

        await process_untagged_events(loop)
        await extract_groupme_events(loop)

        # Safety net: fire any briefing missed while the Mac slept through its
        # scheduled time. This is the first poll to run after wake, so it is
        # where a launchd-kept-alive process recovers a dropped cron briefing.
        _check_and_run_missed_briefings(context.job_queue)

    # ── Check urgent alerts job ───────────────────────────────────────────────

    async def check_urgent_alerts_job(context) -> None:
        cur = conn.execute(
            "SELECT id, source, title, body FROM events WHERE urgency='URGENT' AND notified=0"
        )
        rows = cur.fetchall()
        for row in rows:
            event_id, source, title, body = row
            text = f"🚨 Urgent — {source}\n{title}"
            clean_body = _strip_internal_tags(body) if body else ""
            if clean_body:
                text += f"\n{clean_body[:300]}"
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
                conn.execute("UPDATE events SET notified=1 WHERE id=?", (event_id,))
                activity.record_urgent_alert(
                    conn, source=source or "", source_ref=event_id,
                    body=f"{title}\n{clean_body}".strip(),
                )
            except Exception as e:
                logger.error(f"Urgent alert send failed: {e}")
        if rows:
            conn.commit()

        # Briefing catch-up rides this 60s job, not the 15-min poll: the
        # user-facing post-wake latency target is ≤60s, and the 15-min poll
        # itself gets misfire-skipped after long sleeps. The helper is cheap and
        # idempotent (sent-key lock), so calling it every minute is safe.
        _check_and_run_missed_briefings(context.job_queue)

    # ── Evening briefing job ──────────────────────────────────────────────────

    async def briefing_job(context) -> None:
        job_name = getattr(context.job, "name", "") or ""
        is_override = job_name == "evening_briefing_override_job"
        is_catchup  = job_name == "evening_briefing_catchup_job"
        if is_override:
            state.delete(conn, "evening_briefing_override")
        elif not is_catchup:
            ovr = state.get(conn, "evening_briefing_override")
            if ovr:
                try:
                    ovr_dt = datetime.datetime.fromisoformat(ovr)
                    if ovr_dt.date() == datetime.datetime.now(local_tz).date():
                        logger.info(
                            f"Evening briefing skipped at default time — "
                            f"override active for {ovr_dt.strftime('%H:%M %Z')}"
                        )
                        return
                except ValueError:
                    pass
            if not _within(EVENING_WINDOW):
                now_local = datetime.datetime.now(local_tz)
                logger.warning(
                    f"Evening briefing fired at {now_local.strftime('%H:%M %Z')} — "
                    f"outside {EVENING_WINDOW[0]:02d}:00–{EVENING_WINDOW[1]:02d}:00 "
                    f"window. Skipping (likely tz misconfig)."
                )
                return
        today = datetime.date.today()
        # Claim the slot BEFORE the slow compose, not after the send — see the
        # morning job for the full rationale. Without this, a compose taking
        # >60s lets the 60s catch-up net queue a duplicate evening briefing.
        # is_catchup already holds the lock from _check_and_run_missed_briefings.
        if not is_catchup:
            if state.get(conn, "last_evening_briefing_sent") == today.isoformat():
                return  # already sent today
            state.set(conn, "last_evening_briefing_sent", today.isoformat())
        loop = asyncio.get_running_loop()
        # Pre-fetch a known-complete dataset (calendar/canvas/weather/groupme)
        # so the composer works from injected context instead of tool calls.
        bundle = await loop.run_in_executor(
            None, briefings.bundle_briefing_context, "evening", config, conn
        )
        response = await loop.run_in_executor(
            None, briefings.compose_evening, agent, bundle
        )
        if response:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Good evening, sir.\n\n{response}",
                )
                _record_briefing_sent("evening", response, job_name, bh, bm)
            except Exception as e:
                logger.error(f"Evening briefing send failed: {e}")
                # Release the slot so the catch-up net can retry a failed send.
                state.delete(conn, "last_evening_briefing_sent")
        else:
            # Empty compose isn't a real send — don't let the lock suppress retry.
            state.delete(conn, "last_evening_briefing_sent")

    # ── Missed-briefing catch-up (poll-driven safety net) ─────────────────────
    # APScheduler drops cron jobs whose run time was missed beyond their grace
    # period, and launchd KeepAlive keeps this same process alive across the
    # Mac's sleep/wake — so the old startup-only catch-up never re-ran on wake
    # and briefings were silently lost (last_evening_briefing_sent stuck days
    # behind). This helper runs on every poll tick (and once at startup) to fire
    # a still-timely missed briefing late, or skip it once it has gone stale.
    #
    # last_<kind>_briefing_sent is the idempotency lock: it is set to today
    # synchronously here, BEFORE the briefing job is queued, so repeat calls in
    # the same day are no-ops. The on-time window (run time .. +60s) is left to
    # the run_daily job, which owns it and sets the same lock on success.
    #
    # Scenario trace (see requirement 7):
    #   A. On-time fire — run_daily fires at the scheduled time and sets
    #      last_*_briefing_sent=today. Next poll: sent==today → skip. A poll
    #      landing in the first 60s (before run_daily finishes) sees age_min<1
    #      → skip, so it never races run_daily. No double-send.
    #   B. Wake 30 min late — run_daily misfired while asleep. First poll after
    #      wake: sent!=today, age_min=30 (1..max), no override → "Catching up",
    #      lock taken, briefing job queued → fires ~1s later. One briefing.
    #   C. Wake next morning after sleeping through the evening — the evening
    #      slot is scheduled for *today* 20:00, which is in the future at 07:00
    #      (age_min<1) → skip; yesterday's missed evening briefing is not
    #      resurrected. A morning briefing woken to well past its time (e.g.
    #      09:30 vs a 07:00 slot, 150>max) → "Skipping stale", lock set so it
    #      will not retrigger for the rest of the day.
    #   D. Two polls in a row after a late wake — the first sets the lock before
    #      queueing; the second sees sent==today → skip. Fires exactly once.
    def _check_and_run_missed_briefings(job_queue) -> None:
        now_local = datetime.datetime.now(local_tz)
        today     = now_local.date()
        agent_cfg = config.get("agent") or {}
        try:
            default_max = int(agent_cfg.get("briefing_catchup_max_minutes", 120))
        except (TypeError, ValueError):
            default_max = 120

        def _slot_max(key: str) -> int:
            """Per-slot catch-up window, falling back to the shared default."""
            try:
                return int(agent_cfg.get(key, default_max))
            except (TypeError, ValueError):
                return default_max

        def _override_active_today(kind: str) -> bool:
            """True if a still-future override for today owns this slot."""
            ovr = state.get(conn, f"{kind}_briefing_override")
            if not ovr:
                return False
            try:
                ovr_dt = datetime.datetime.fromisoformat(ovr)
            except ValueError:
                return False
            return ovr_dt.date() == today and ovr_dt > now_local

        acted = False
        for kind, runner, sched_h, sched_m, sent_key, max_key in (
            ("morning", morning_briefing_job, mbh, mbm, "last_morning_briefing_sent",
             "morning_briefing_catchup_max_minutes"),
            ("evening", briefing_job,         bh,  bm,  "last_evening_briefing_sent",
             "evening_briefing_catchup_max_minutes"),
        ):
            if state.get(conn, sent_key) == today.isoformat():
                continue  # already sent today (on-time or an earlier catch-up)
            scheduled = now_local.replace(
                hour=sched_h, minute=sched_m, second=0, microsecond=0,
            )
            age_min = int((now_local - scheduled).total_seconds() // 60)
            if age_min < 1:
                # Not due yet, or within the on-time minute owned by run_daily.
                continue
            if _override_active_today(kind):
                continue
            acted = True
            max_min = _slot_max(max_key)
            if age_min > max_min:
                logger.warning(
                    f"Skipping stale {kind} briefing — {age_min} min late "
                    f"(max {max_min})"
                )
                # Mark sent so we don't re-evaluate this slot on every poll today.
                state.set(conn, sent_key, today.isoformat())
                continue
            logger.info(f"Catching up {kind} briefing, {age_min} minutes late")
            # Take the lock before queueing so a second call can't double-fire;
            # the runner re-sets it on a successful send too.
            state.set(conn, sent_key, today.isoformat())
            job_queue.run_once(runner, when=1, name=f"{kind}_briefing_catchup_job")

        if not acted:
            # DEBUG, not INFO: this fires on the 60s carrier, so an INFO line
            # here would emit ~1,440 no-op lines/day and drown the log.
            logger.debug("No missed briefings")

    # ── Nightly activity-table cleanup ────────────────────────────────────────
    # Trims the activity-capture tables (llm_exchanges, tool_calls, briefings_sent,
    # urgent_alerts_sent) to the last 30 days so they don't grow without bound.
    async def cleanup_activity_job(context) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, activity.cleanup_old_activity, conn, 30)

    # ── Build and run application ─────────────────────────────────────────────

    app = (
        Application.builder()
        .token(bot_token)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler.on_message))
    # Photos / PDFs → agent.on_media: vision extraction, then the same
    # gated_write approval card GroupMe scheduling uses.
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.PDF, handler.on_media))
    app.add_handler(CallbackQueryHandler(handler.on_callback))

    # Late-bind for the reschedule_briefing tool — it needs the live job_queue
    # and the briefing runners to schedule one-shot overrides at message time.
    agent.job_queue = app.job_queue
    agent._morning_briefing_runner = morning_briefing_job
    agent._evening_briefing_runner = briefing_job

    app.job_queue.run_daily(
        morning_briefing_job,
        time=datetime.time(mbh, mbm, tzinfo=local_tz),
    )
    app.job_queue.run_daily(
        briefing_job,
        time=datetime.time(bh, bm, tzinfo=local_tz),
    )
    app.job_queue.run_repeating(poll_connectors_job,    interval=900, first=60)
    app.job_queue.run_repeating(check_urgent_alerts_job, interval=60,  first=10)
    app.job_queue.run_daily(
        cleanup_activity_job,
        time=datetime.time(3, 0, tzinfo=local_tz),
    )

    # Restore any pending briefing override that survived a restart. The
    # system_state row persists across restarts, but the in-memory one-shot
    # does not — re-queue it here (or clear it if the time has already passed).
    for kind, runner in (
        ("morning", morning_briefing_job),
        ("evening", briefing_job),
    ):
        ovr = state.get(conn, f"{kind}_briefing_override")
        if not ovr:
            continue
        try:
            ovr_dt = datetime.datetime.fromisoformat(ovr)
        except ValueError:
            logger.warning(f"Discarding malformed {kind} override: {ovr!r}")
            state.delete(conn, f"{kind}_briefing_override")
            continue
        if ovr_dt <= datetime.datetime.now(local_tz):
            logger.info(
                f"{kind.capitalize()} override at {ovr_dt.isoformat()} "
                f"already elapsed — clearing."
            )
            state.delete(conn, f"{kind}_briefing_override")
            continue
        app.job_queue.run_once(
            runner, when=ovr_dt, name=f"{kind}_briefing_override_job",
        )
        logger.info(
            f"Restored {kind} briefing override for "
            f"{ovr_dt.strftime('%Y-%m-%d %H:%M %Z')}"
        )

    # Missed-briefing catch-up at startup. Same helper the poll job uses, so
    # boot and wake-from-sleep recover a dropped briefing identically.
    _check_and_run_missed_briefings(app.job_queue)

    logger.info(f"Morning briefing scheduled at {mbt_str} {tz_name}")
    logger.info(f"Evening briefing scheduled at {bt_str} {tz_name}")
    logger.info("Running. Ctrl+C to stop.")

    # Windows: asyncio's Proactor loop has no add_signal_handler, so passing
    # explicit stop_signals would crash PTB at startup. Omit them — PTB then
    # falls back to KeyboardInterrupt, which is exactly what the dashboard's
    # restart endpoint raises via signal.raise_signal(SIGINT).
    run_kwargs = {"drop_pending_updates": False}
    if not compat.IS_WINDOWS:
        run_kwargs["stop_signals"] = (signal.SIGINT, signal.SIGTERM)
    app.run_polling(**run_kwargs)


if __name__ == "__main__":
    main()
