"""
friday.py
Project Friday — entry point.

PTB Application owns the main event loop.
All scheduling goes through job_queue — no secondary threads, no schedule library.
"""

import asyncio
import datetime
import logging
import os
import signal
import sys

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

    agent   = FridayAgent(config)
    handler = TelegramHandler(config, agent, conn)

    bot_token  = handler.bot_token
    chat_id    = handler.chat_id
    agent_cfg  = config.get("agent", {})
    bt_str     = agent_cfg.get("briefing_time", "21:45")
    bh, bm     = (int(x) for x in bt_str.split(":"))
    mbt_str    = agent_cfg.get("morning_briefing_time", "08:00")
    mbh, mbm   = (int(x) for x in mbt_str.split(":"))

    # ── Post-init: startup message + initial state ────────────────────────────

    async def post_init(app: Application) -> None:
        state.set(conn, "status",     "running")
        state.set(conn, "started_at", datetime.datetime.now().isoformat())
        state.set(conn, "provider",   config.get("provider", "ollama"))
        state.set(conn, "model",      agent.model_name)
        await app.bot.send_message(
            chat_id=chat_id,
            text=f"⚡ Friday online — {config.get('provider', 'ollama')} / {agent.model_name}",
        )
        logger.info("Startup complete.")

    # ── Post-stop: offline message ────────────────────────────────────────────

    async def post_stop(app: Application) -> None:
        state.set(conn, "status", "stopped")
        try:
            await app.bot.send_message(chat_id=chat_id, text="Friday going offline. 🔴")
        except Exception:
            pass

    # ── Morning briefing job ─────────────────────────────────────────────────

    async def morning_briefing_job(context) -> None:
        today = datetime.datetime.now().strftime("%A, %B %d")
        weather_ctx = ""
        wx = weather_connector.respond(config.get("weather", {}), "weather")
        if wx:
            weather_ctx = f" {wx}"
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, agent._think,
            f"Compose a brief morning briefing for {today}.{weather_ctx} "
            f"Start with 'Good morning, sir. Here is your day:' and keep it under 4 sentences."
        )
        if response:
            await context.bot.send_message(chat_id=chat_id, text=f"🌅 {response}")

    # ── Poll connectors job ───────────────────────────────────────────────────

    async def poll_connectors_job(context) -> None:
        logger.info("Polling connectors...")
        # Canvas and GroupMe connectors will be wired here in Phase 2/4

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
            await context.bot.send_message(chat_id=chat_id, text=text)
            conn.execute("UPDATE events SET notified=1 WHERE id=?", (event_id,))
        if rows:
            conn.commit()

    # ── Evening briefing job ──────────────────────────────────────────────────

    async def briefing_job(context) -> None:
        today = datetime.datetime.now().strftime("%A, %B %d")
        weather_ctx = ""
        wx = weather_connector.fetch(config.get("weather", {}))
        if wx:
            weather_ctx = f" Current weather: {wx}."
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, agent._think,
            f"Compose a brief evening briefing for {today}.{weather_ctx} "
            f"Keep it under 5 sentences. Plain prose only."
        )
        if response:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📅 Evening Briefing — {today}\n\n{response}",
            )

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

    app.job_queue.run_daily(
        morning_briefing_job,
        time=datetime.time(mbh, mbm, tzinfo=datetime.timezone.utc),
    )
    app.job_queue.run_daily(
        briefing_job,
        time=datetime.time(bh, bm, tzinfo=datetime.timezone.utc),
    )
    app.job_queue.run_repeating(poll_connectors_job,    interval=900, first=60)
    app.job_queue.run_repeating(check_urgent_alerts_job, interval=60,  first=10)

    logger.info(f"Morning briefing scheduled at {mbt_str} UTC")
    logger.info(f"Evening briefing scheduled at {bt_str} UTC")
    logger.info("Running. Ctrl+C to stop.")

    app.run_polling(
        drop_pending_updates=False,
        stop_signals=(signal.SIGINT, signal.SIGTERM),
    )


if __name__ == "__main__":
    main()
