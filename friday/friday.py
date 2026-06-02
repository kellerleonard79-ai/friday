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
import signal
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters

_HERE = os.path.dirname(os.path.abspath(__file__))

os.makedirs(os.path.join(_HERE, "logs"), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(_HERE, "logs", "friday.log")),
    ],
)
logger = logging.getLogger("friday")

from agent.core import FridayAgent
from channels.telegram import TelegramHandler
from memory.db import Database
import memory.state as state
from connectors import weather as weather_connector
from connectors import canvas as canvas_connector
from connectors import gcal_sync
from connectors import groupme as groupme_connector
from connectors import apple_calendar as apple_cal
from agent import briefings
from actions import calendar as apple_writer
from dashboard import server as dashboard_server


def load_config() -> dict:
    path = os.path.join(_HERE, "friday_config.yaml")
    if not os.path.exists(path):
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


def main() -> None:
    logger.info("=" * 50)
    logger.info("  Project Friday — Starting up")
    logger.info(f"  {datetime.datetime.now().strftime('%A, %B %d %Y %H:%M')}")
    logger.info("=" * 50)

    config = load_config()
    check_environment(config)

    memory_cfg  = config.get("memory", {})
    db_path     = os.path.join(_HERE, memory_cfg.get("db_path", "memory/friday_memory.db"))
    db          = Database(db_path)
    conn        = db.connection()

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
        config_path = Path(_HERE) / "friday_config.yaml"
        try:
            server = await dashboard_server.start_server(config_path, conn)
            task = asyncio.create_task(server.serve(), name="dashboard_server")
            app.bot_data["dashboard_server"] = server
            app.bot_data["dashboard_task"] = task
        except Exception as e:
            logger.error(f"Dashboard server failed to start: {e}")

        await app.bot.send_message(
            chat_id=chat_id,
            text=f"⚡ Friday online — {config.get('provider', 'ollama')} / {agent.model_name}",
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
            await app.bot.send_message(chat_id=chat_id, text="Friday going offline. 🔴")
        except Exception:
            pass
        conn.close()

    # ── Morning briefing job ─────────────────────────────────────────────────

    async def morning_briefing_job(context) -> None:
        is_override = getattr(context.job, "name", "") == "morning_briefing_override_job"
        if is_override:
            state.delete(conn, "morning_briefing_override")
        else:
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
        loop = asyncio.get_running_loop()
        today_evts = await loop.run_in_executor(
            None, apple_cal.events_for_day, config, today
        )
        upcoming_evts = await loop.run_in_executor(
            None, apple_cal.events_in_window, config, today, today + datetime.timedelta(days=7)
        )
        wx = await loop.run_in_executor(
            None, weather_connector.respond, config.get("weather", {}), ""
        )
        response = await loop.run_in_executor(
            None, briefings.compose_morning, agent, today_evts, upcoming_evts, wx
        )
        if response:
            try:
                await context.bot.send_message(chat_id=chat_id, text=f"🌅 {response}")
            except Exception as e:
                logger.error(f"Morning briefing send failed: {e}")

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
                None, lambda: agent._think(prompt, use_tools=False)
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
                None, lambda p=prompt: agent._think(p, use_tools=False)
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
                    None, canvas_connector.sync_to_apple_calendar, config, conn,
                )
                if synced:
                    logger.info(f"Canvas: {synced} due date(s) written to Apple Calendar.")
            except Exception as e:
                logger.error(f"Canvas poll failed: {e}")

        gcal_cfg = config.get("gcal_sync") or {}
        if gcal_cfg.get("calendars"):
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

    # ── Check urgent alerts job ───────────────────────────────────────────────

    async def check_urgent_alerts_job(context) -> None:
        cur = conn.execute(
            "SELECT id, source, title, body FROM events WHERE urgency='URGENT' AND notified=0"
        )
        rows = cur.fetchall()
        for row in rows:
            event_id, source, title, body = row
            text = f"🚨 Urgent — {source}\n{title}"
            if body:
                text += f"\n{body[:300]}"
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
                conn.execute("UPDATE events SET notified=1 WHERE id=?", (event_id,))
            except Exception as e:
                logger.error(f"Urgent alert send failed: {e}")
        if rows:
            conn.commit()

    # ── Evening briefing job ──────────────────────────────────────────────────

    async def briefing_job(context) -> None:
        is_override = getattr(context.job, "name", "") == "evening_briefing_override_job"
        if is_override:
            state.delete(conn, "evening_briefing_override")
        else:
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
        tomorrow = today + datetime.timedelta(days=1)
        loop = asyncio.get_running_loop()
        tomorrow_evts = await loop.run_in_executor(
            None, apple_cal.events_for_day, config, tomorrow
        )
        upcoming_evts = await loop.run_in_executor(
            None, apple_cal.events_in_window, config, tomorrow,
            tomorrow + datetime.timedelta(days=7),
        )
        canvas_pending = conn.execute(
            "SELECT title, due_at, urgency FROM events "
            "WHERE source='canvas' AND urgency IN ('URGENT','SOON') AND notified=0 "
            "ORDER BY due_at"
        ).fetchall()
        wx = await loop.run_in_executor(
            None, weather_connector.respond, config.get("weather", {}), ""
        )
        response = await loop.run_in_executor(
            None, briefings.compose_evening,
            agent, tomorrow_evts, upcoming_evts, canvas_pending, wx,
        )
        if response:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"📅 Evening Briefing — {today.strftime('%A, %B %-d')}\n\n{response}",
                )
            except Exception as e:
                logger.error(f"Evening briefing send failed: {e}")

    # ── Build and run application ─────────────────────────────────────────────

    app = (
        Application.builder()
        .token(bot_token)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler.on_message))
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

    logger.info(f"Morning briefing scheduled at {mbt_str} {tz_name}")
    logger.info(f"Evening briefing scheduled at {bt_str} {tz_name}")
    logger.info("Running. Ctrl+C to stop.")

    app.run_polling(
        drop_pending_updates=False,
        stop_signals=(signal.SIGINT, signal.SIGTERM),
    )


if __name__ == "__main__":
    main()
