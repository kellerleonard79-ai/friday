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

# Internal "[priority=<tier>]" tag the GroupMe connector prepends to each event
# body purely as a signal for the LLM's urgency pass and the briefing/urgent SQL
# LIKE queries. Tiers are defined in connectors/groupme.py. It must stay in
# events.body, but it is noise in any user-facing message — strip it (and its
# now-orphaned blank line) before the text reaches Telegram.
_PRIORITY_TAG = re.compile(r"^\[priority=[^\]]*\]\n?", re.MULTILINE)


def _strip_internal_tags(body: str) -> str:
    """Remove internal LLM-only tags from an event body for user-facing display."""
    return _PRIORITY_TAG.sub("", body).strip()


from agent.core import FridayAgent
from llm import dispatch as llm_dispatch
from llm import profiles as llm_profiles
from channels.telegram import TelegramHandler
from channels.dashboard import DashboardChannel
from memory.db import Database
import memory.activity as activity
import memory.state as state
from calendars import backend as calendar_backend
from tools import calendar_read as tool_calendar
from effects import pending as pending_actions
from tools import calendar_write as tool_calendar_write
from tools import work_write as tool_work_write
from connectors import canvas as canvas_connector
from connectors import gcal_sync
from connectors import groupme as groupme_connector
from connectors import location
from agent import briefings
from actions import calendar as apple_writer
from dashboard import server as dashboard_server
import power

# Names of the two recurring briefing jobs. They lived in agent/tools.py while
# the update_setting tool needed to replace the jobs in place; that tool is
# gone, so they live with the only remaining registrant.
MORNING_BRIEFING_JOB = "morning_briefing_daily"
EVENING_BRIEFING_JOB = "evening_briefing_daily"


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
#                                                GroupMe. The LLM tagging and
#                                                GroupMe event extraction that
#                                                followed are torn down and
#                                                currently no-ops.)
#       run_repeating→ canvas_health_job        (5 min: Canvas cache refresh
#                                                only, so a dead REST token or
#                                                a stale period card is caught
#                                                within minutes of wake, not
#                                                up to 15 of them)
#       run_repeating→ check_urgent_alerts_job  (1 min: fire URGENT interrupts)
#       run_repeating→ power_reconcile_job      (1 min: passing-period wake
#                                                hold — see power.py)
#       run_daily    → power_materialize_job    (3:20am: materializes the next
#                                                few days' pmset wakeorpoweron
#                                                entries; also runs once at
#                                                startup)
#   • One-shot briefing overrides + missed-briefing catch-up
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

    # Every LLM call in the process goes through this one dispatcher.
    llm_dispatch.configure(config, conn=conn)

    # The tool layer's read path needs the config the calendar backend reads
    # through. Registration itself happens on import (agent/turn.py imports the
    # tool modules), so this only hands over config, never builds the registry.
    tool_calendar.configure(config)
    # The write tools need the connection as well: a tool's arguments are the
    # model's, so a database handle cannot be passed per call.
    tool_calendar_write.configure(config, conn=conn)
    # The to-do tool needs only the connection — a task write has no config
    # knob of its own, unlike the calendar tools' default-calendar lookup.
    tool_work_write.configure(conn=conn)
    # The card TTL and the stale threshold. confirm() is reached from a
    # button tap and has nowhere on that path for a config to travel.
    pending_actions.configure(config)

    agent   = FridayAgent(config, conn=conn)
    handler = TelegramHandler(config, agent, conn)
    agent.telegram_handler = handler  # late-bound for the media → gated_write path

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
        state.set(conn, "model",      llm_profiles.get("CHAT").model)
        # Honor pre-existing pause state across restarts; default to unpaused.
        if state.get(conn, "paused") is None:
            state.set(conn, "paused", "false")

        # A disablesleep flag stranded by a crash or a SIGKILL must not
        # survive into this run — see power.startup_clear's docstring on why
        # this is unconditional rather than read-gated. Off the loop: it
        # shells out to pmset.
        await asyncio.get_running_loop().run_in_executor(
            None, power.startup_clear, conn)

        # Dashboard web server — runs inside this same asyncio loop. No threads,
        # no second event loop. Honors the single-loop rule in CLAUDE.md.
        config_path = paths.config_path()
        try:
            # One group per protocol: loopback HTTP always, plus a tailnet
            # group that is HTTPS when a Tailscale cert is available and
            # plain HTTP otherwise — see start_server()'s docstring.
            dashboard_groups = await dashboard_server.start_server(
                config_path, conn,
                # Bound here rather than at definition: app.bot only exists once
                # PTB has built the Application.
                on_demand_briefing=lambda: send_on_demand_briefing(app.bot),
            )
            dashboard_tasks = [
                asyncio.create_task(server.serve(sockets=sockets), name="dashboard_server")
                for server, sockets in dashboard_groups
            ]
            # _publish_dashboard / _dashboard_notify reach through
            # server.config.app, which is the same FastAPI instance on
            # every server in the list — any one of them works, so the
            # first (loopback) stays the singular handle those use.
            app.bot_data["dashboard_server"] = dashboard_groups[0][0]
            app.bot_data["dashboard_servers"] = [server for server, _ in dashboard_groups]
            app.bot_data["dashboard_tasks"] = dashboard_tasks
        except Exception as e:
            logger.error(f"Dashboard server failed to start: {e}")

        # Best-effort, like the offline message in post_stop. A bad chat_id or a
        # transient Telegram error must not abort post_init — that kills the core
        # seconds after boot and takes the dashboard down with it, so the menubar
        # sees connection-refused on 5174 while the supervisor restart-loops.
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"JARVIS online — {config.get('provider', 'ollama')} / {llm_profiles.get('CHAT').model}",
            )
        except Exception as e:
            logger.error(f"Startup message failed (check telegram.chat_id): {e}")
        logger.info("Startup complete.")

    # ── Post-stop: offline message ────────────────────────────────────────────

    async def post_stop(app: Application) -> None:
        state.set(conn, "status", "stopped")
        # Cleared on every shutdown path, graceful or not-quite — a machine
        # that cannot sleep because the daemon exited mid-block is a dead
        # battery in a backpack. Best-effort: if pmset itself is wedged this
        # must not block the rest of shutdown.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, power.shutdown_clear, conn)
        except Exception as e:
            logger.warning(f"power: shutdown_clear failed: {e}")
        for server in app.bot_data.get("dashboard_servers", []):
            server.should_exit = True
        tasks = app.bot_data.get("dashboard_tasks", [])
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
            except (asyncio.TimeoutError, Exception):
                pass
        try:
            await app.bot.send_message(chat_id=chat_id, text="JARVIS going offline.")
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
        # Pre-fetch a known-complete dataset (calendar/canvas/weather/groupme).
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
                    # The deterministic renderer has no greeting to swap, so
                    # the late note is simply prepended.
                    response = (
                        "Running a little late this morning, sir — the machine "
                        f"was asleep until just now.\n\n{response}"
                    )
            try:
                await context.bot.send_message(chat_id=chat_id, text=response)
                _record_briefing_sent("morning", response, job_name, mbh, mbm)
            except Exception as e:
                logger.error(f"Morning briefing send failed: {e}")
                # Release the slot so the catch-up net can retry a failed send.
                state.delete(conn, "last_morning_briefing_sent")
        else:
            # Empty compose isn't a real send — don't let the lock suppress retry.
            # Unreachable while compose_morning is a deterministic renderer.
            state.delete(conn, "last_morning_briefing_sent")

    # ── LLM urgency tagging for unprocessed events ───────────────────────────

    async def process_untagged_events(loop) -> None:
        """DISABLED pending the LLM rewrite (branch llm-layer-teardown).

        Deliberately does NOT tag rows NORMAL and does NOT set processed=1.
        Marking rows scanned while the scanner is offline would silently
        discard every event that arrives during the teardown window — the new
        tagger has to be able to backfill the whole backlog, and processed=0
        is the only record that a row was never looked at.

        Consequence, accepted: nothing is ever tagged URGENT, so
        check_urgent_alerts_job finds nothing and urgent alerts stay silent.
        """
        logger.info("Urgency tagging disabled pending the LLM rewrite — "
                    "leaving unprocessed events untouched.")

    # ── Event extraction from groupme messages ───────────────────────────────

    async def extract_groupme_events(loop) -> None:
        """DISABLED pending the LLM rewrite (branch llm-layer-teardown).

        Same contract as process_untagged_events: event_extracted stays 0, so
        the backlog is intact for the new extractor. GroupMe therefore stops
        producing gated_write approval cards. The card machinery itself is
        untouched — it just has no producer on this path.
        """
        logger.info("GroupMe event extraction disabled pending the LLM "
                    "rewrite — leaving unscanned rows untouched.")

    # ── Poll connectors job ───────────────────────────────────────────────────

    def _publish_dashboard(app, event: dict) -> None:
        """Push one event to any attached browser, if a dashboard is running.

        Best-effort by construction: the dashboard is optional (it may have
        failed to start, and friday.py carries on when it does), so every hop
        to it is guarded. A poll must never fail because nobody is looking.
        """
        try:
            server = app.bot_data.get("dashboard_server")
            if server is None:
                return
            # config.app is the FastAPI instance as handed to uvicorn.
            # config.loaded_app is that same app wrapped in middleware, which
            # has no .state — reaching through it is how this goes silently
            # dead the first time it is refactored.
            server.config.app.state.broadcaster.publish(event)
        except Exception as e:
            logger.debug(f"dashboard publish skipped: {e}")

    def _dashboard_notify(app, title: str, text: str) -> bool:
        """The dashboard's own interrupt path — channels/dashboard.py's two
        doors (an in-browser Web Notification via the SSE stream, plus a
        native macOS notification via osascript for when no tab is open).
        Same guard as _publish_dashboard: best-effort, dashboard optional.
        Returns True if either door opened, mirroring DashboardChannel.notify.
        """
        try:
            server = app.bot_data.get("dashboard_server")
            if server is None:
                return False
            broadcaster = server.config.app.state.broadcaster
            return DashboardChannel(sink=broadcaster.publish).notify(title, text)
        except Exception as e:
            logger.debug(f"dashboard notify skipped: {e}")
            return False

    async def _maybe_alert_canvas_token(app, bot, cache: dict, loop) -> None:
        # A LATCH, not a window — same reasoning as check_urgent_alerts_job's
        # `notified` column: this fires from two different jobs on two
        # different cadences against the same state key, so anything short
        # of permanent-until-recovered is a loop or a double-send.
        #
        # Set only once at least one door actually opened, on either channel —
        # if Telegram is blocked (school Wi-Fi) but the dashboard notified, or
        # the dashboard isn't open but Telegram got through, that is a
        # delivered alert. If BOTH failed (Mac asleep with nobody watching
        # either surface), nothing is latched and the next cycle retries.
        alert_key = "canvas_token_alert_sent"
        if cache.get("rest_auth_expired"):
            if state.get(conn, alert_key) != "true":
                text = briefings.canvas_token_expired_alert()
                delivered = False
                try:
                    await bot.send_message(chat_id=chat_id, text=text)
                    delivered = True
                except Exception as e:
                    logger.error(f"Canvas token-expired Telegram alert failed: {e}")
                # _dashboard_notify's osascript door is a blocking subprocess
                # call — off the loop, same as every other blocking call in
                # these jobs, so a hung osascript can't stall the scheduler.
                if await loop.run_in_executor(
                        None, _dashboard_notify, app, "Canvas API expired", text):
                    delivered = True
                if delivered:
                    state.set(conn, alert_key, "true")
        elif cache.get("rest_ok") and state.get(conn, alert_key) == "true":
            state.delete(conn, alert_key)

    async def canvas_health_job(context) -> None:
        """A narrow, frequent Canvas-only refresh, separate from the 15-min
        poll_connectors_job specifically so a dead REST token — or a stale
        period card — is caught within minutes of the Mac waking rather than
        up to 15 of them; poll_connectors_job itself gets misfire-skipped
        after a long sleep (see check_urgent_alerts_job above). Only touches
        the read-only cache refresh() already used for the period card —
        fetch()/sync_to_calendar() (the calendar-write path) stay on the
        15-min job."""
        canvas_cfg = config.get("canvas", {})
        if not canvas_cfg.get("ical_url"):
            return
        loop = asyncio.get_running_loop()
        try:
            cache = await loop.run_in_executor(
                None, canvas_connector.refresh, config, conn)
            await _maybe_alert_canvas_token(app, context.bot, cache, loop)
            _publish_dashboard(app, {"kind": "canvas",
                                     "refreshed_at": cache.get("refreshed_at", "")})
        except Exception as e:
            logger.error(f"Canvas health check failed: {e}")

    async def poll_connectors_job(context) -> None:
        logger.info("Polling connectors...")
        loop = asyncio.get_running_loop()

        # Refresh the machine's location cache. Nothing reads it while the
        # prompt layer is torn down (the injection lived in
        # _system_instruction), but the warm is cheap and the rewrite needs a
        # populated cache on day one — a cold lookup blocks for seconds.
        await loop.run_in_executor(None, location.warm)

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
                # The period card's cache. Separate from the two calls above on
                # purpose: those write due dates into the calendar, this fills
                # the tables the dashboard reads. refresh() never raises, so a
                # dead Canvas degrades the card and leaves the rest of the poll
                # alone.
                cache = await loop.run_in_executor(
                    None, canvas_connector.refresh, config, conn,
                )
                logger.info(
                    f"Canvas cache: {cache['courses']} course(s), "
                    f"{cache['assignments']} item(s); "
                    f"ical={'ok' if cache['ical_ok'] else 'FAILED'} "
                    f"rest={'ok' if cache['rest_ok'] else 'unavailable'}"
                    + (f" — {cache['error']}" if cache["error"] else "")
                )
                # Tell any open dashboard that the numbers moved. A NUDGE, NOT
                # A PAYLOAD: the browser re-reads /api/schedule, which is the
                # same thing it does on load, so there is one code path that
                # fills the card and no chance of the stream and the endpoint
                # disagreeing. Consistent with stream.py — the stream is not
                # the record.
                _publish_dashboard(app, {"kind": "canvas",
                                         "refreshed_at": cache.get("refreshed_at", "")})
                await _maybe_alert_canvas_token(app, context.bot, cache, loop)
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
        # notifications.groupme_polling is the kill switch (dashboard: "GroupMe
        # polling"). Off means Friday never fetches — no new rows, so nothing
        # for the urgency tagger or event extractor to spend LLM calls on.
        # Existing rows and already-scheduled events are untouched.
        groupme_enabled = (config.get("notifications") or {}).get("groupme_polling", True)
        if not groupme_enabled:
            logger.info("GroupMe: polling disabled (notifications.groupme_polling=false).")
        elif groupme_cfg.get("api_token") and groupme_cfg.get("groups"):
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
        # `notified = 0` is policy/suppression.py::already_alerted, inverted
        # for SQL. It is a LATCH, not a window: this job runs every 60 seconds
        # against the same table, so anything short of permanent is a loop.
        # The windowed rule beside it in that module is a different question
        # and deliberately does not replace this one.
        cur = conn.execute(
            "SELECT id, source, title, body FROM events "
            "WHERE urgency='URGENT' AND notified = 0"
        )
        rows = cur.fetchall()
        for row in rows:
            event_id, source, title, body = row
            clean_body = _strip_internal_tags(body) if body else ""
            # Deterministic, unconditionally. compose_urgent_alert (the LLM
            # path) is gone; this was already its failure fallback.
            text = briefings.fallback_urgent_alert(
                source or "", title or "", clean_body
            )
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
                conn.execute("UPDATE events SET notified=1 WHERE id=?", (event_id,))
                activity.record_urgent_alert(
                    conn, source=source or "", source_ref=event_id,
                    body=text,
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
        # Pre-fetch a known-complete dataset (calendar/canvas/weather/groupme).
        bundle = await loop.run_in_executor(
            None, briefings.bundle_briefing_context, "evening", config, conn
        )
        response = await loop.run_in_executor(
            None, briefings.compose_evening, agent, bundle
        )
        if response:
            try:
                await context.bot.send_message(chat_id=chat_id, text=response)
                _record_briefing_sent("evening", response, job_name, bh, bm)
            except Exception as e:
                logger.error(f"Evening briefing send failed: {e}")
                # Release the slot so the catch-up net can retry a failed send.
                state.delete(conn, "last_evening_briefing_sent")
        else:
            # Empty compose isn't a real send — don't let the lock suppress retry.
            # Unreachable while compose_evening is a deterministic renderer.
            state.delete(conn, "last_evening_briefing_sent")

    # ── On-demand briefing (dashboard / menubar "Brief Me Now") ───────────────
    # The dashboard used to implement this by POSTing the text "brief me" to
    # sendMessage — but Telegram never echoes a bot's own message back through
    # getUpdates, so on_message() never fired and the button did nothing at all.
    # It has to run the briefing itself and push the result to the chat.
    #
    # Deliberately does NOT call _record_briefing_sent: that sets the
    # last_<slot>_briefing_sent lock, which would make the real scheduled
    # briefing silently skip itself for the rest of the day.
    async def send_on_demand_briefing(bot) -> str:
        # The button is easy to double-click and a compose takes tens of
        # seconds; serialize so the second click waits rather than racing a
        # duplicate briefing into the chat.
        #
        # THE LOCK MOVED to agent/briefings.py and is now shared with the fast
        # path's "brief me", which is the second door onto the same work. A
        # lock private to this closure serialized this button against itself
        # and let a chat briefing assemble the same bundle alongside it. The
        # lock order is documented at the definition.
        async with briefings.ON_DEMAND_LOCK:
            loop = asyncio.get_running_loop()
            bundle = await loop.run_in_executor(
                None, briefings.bundle_briefing_context, "on_demand", config, conn
            )
            response = await loop.run_in_executor(
                None, briefings.compose_on_demand, agent, bundle
            )
            if not response:
                # Unreachable while compose_on_demand is a deterministic renderer.
                raise RuntimeError("Briefing came back empty.")
            await bot.send_message(chat_id=chat_id, text=response)
            return response

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

    # ── Passing-period wake schedule ──────────────────────────────────────────
    # Two jobs for power.py's two mechanisms. Minute-resolution for the hold
    # (self-heals after sleep/crash/config edit — see power.reconcile), daily
    # for materializing the actual OS-level wake events a few days ahead
    # (pmset repeat can't express seven blocks a day; see power.py's
    # docstring). Both are best-effort: a pmset failure here must never take
    # the rest of the poll/job loop down with it.
    async def power_reconcile_job(context) -> None:
        loop = asyncio.get_running_loop()
        cfg = load_config()   # cheap re-read: a dashboard edit takes effect
                              # within one tick rather than needing a restart
        try:
            await loop.run_in_executor(None, power.reconcile, cfg, conn)
        except Exception as e:
            logger.warning(f"power: reconcile failed: {e}")

    async def power_materialize_job(context) -> None:
        loop = asyncio.get_running_loop()
        cfg = load_config()
        try:
            result = await loop.run_in_executor(
                None, power.materialize_wakes, cfg, conn)
            if result.get("added") or result.get("cancelled"):
                logger.info(
                    f"power: materialized wakes — added {len(result.get('added', []))}, "
                    f"cancelled {len(result.get('cancelled', []))}."
                )
            if result.get("failed_add") or result.get("failed_cancel"):
                logger.warning(
                    f"power: {len(result.get('failed_add', []))} wake(s) failed to "
                    f"add, {len(result.get('failed_cancel', []))} failed to cancel — "
                    f"see power.sudoers_line()."
                )
        except Exception as e:
            logger.warning(f"power: materialize_wakes failed: {e}")

    # ── Tailscale HTTPS cert renewal ──────────────────────────────────────────
    # `tailscale cert` is idempotent, so this can run daily with no expiry
    # math on this side — see dashboard/tls_certs.py and
    # check_and_renew_tailscale_cert's docstring for why a renewal also
    # triggers a restart.
    async def cert_renewal_job(context) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, dashboard_server.check_and_renew_tailscale_cert)

    # ── Build and run application ─────────────────────────────────────────────

    # concurrent_updates lets callback taps (Confirm/Cancel) and new messages
    # start processing while a slow handler runs — without it PTB awaits each
    # update sequentially and one hung handler bricks the whole bot (July 9
    # outage). LLM work is still serialized: the Semaphore(1) in
    # channels/telegram.py gates on_message/on_media in FIFO order, so this
    # never produces concurrent Gemini calls from user messages.
    app = (
        Application.builder()
        .token(bot_token)
        .concurrent_updates(True)
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

    # Named so the update_setting tool can find and replace them when the user
    # moves a briefing time permanently — same remove-then-re-register dance
    # reschedule_briefing does for one-off overrides.
    app.job_queue.run_daily(
        morning_briefing_job,
        time=datetime.time(mbh, mbm, tzinfo=local_tz),
        name=MORNING_BRIEFING_JOB,
    )
    app.job_queue.run_daily(
        briefing_job,
        time=datetime.time(bh, bm, tzinfo=local_tz),
        name=EVENING_BRIEFING_JOB,
    )
    app.job_queue.run_repeating(poll_connectors_job,    interval=900, first=60)
    app.job_queue.run_repeating(canvas_health_job,      interval=300, first=45)
    app.job_queue.run_repeating(check_urgent_alerts_job, interval=60,  first=10)
    app.job_queue.run_daily(
        cleanup_activity_job,
        time=datetime.time(3, 0, tzinfo=local_tz),
    )
    app.job_queue.run_daily(
        cert_renewal_job,
        time=datetime.time(3, 10, tzinfo=local_tz),
    )
    app.job_queue.run_repeating(power_reconcile_job, interval=60, first=15)
    app.job_queue.run_daily(
        power_materialize_job,
        time=datetime.time(3, 20, tzinfo=local_tz),
    )
    # Also once at startup — a fresh install or a schedule edit should not
    # have to wait for 3:20am to get its first materialized wake.
    app.job_queue.run_once(power_materialize_job, when=20)

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
